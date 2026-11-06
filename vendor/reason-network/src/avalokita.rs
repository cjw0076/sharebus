//! Avalokita's bounded local native-delivery surface. This deliberately bypasses the network daemon's
//! address guessing, mail retention, hooks, and at-least-once fallback. It does not configure agents.
use crate::{agent, util};
use anyhow::Result;
use rusqlite::{Connection, OptionalExtension, params};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    fs,
    io::{self, BufRead, BufReader, Read, Write},
    path::{Path, PathBuf},
    process::Stdio,
    time::Duration,
};

const CONTRACT: &str = "avalokita.reason/1";
const MAX_LINE: u64 = 96 * 1024;
const MAX_QUEUE_STDOUT: u64 = 4096;

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Binding {
    provider: String,
    native_id: String,
    workspace: PathBuf,
    #[serde(default)]
    registry_path: Option<PathBuf>,
    #[serde(default)]
    pid: Option<u32>,
    #[serde(default)]
    started_at_ms: Option<i64>,
    #[serde(default)]
    socket_path: Option<PathBuf>,
    #[serde(default)]
    rollout_path: Option<PathBuf>,
    #[serde(default)]
    executable: Option<PathBuf>,
    #[serde(default)]
    executable_sha256: Option<String>,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Request {
    contract: String,
    dispatch_id: String,
    source_actor_id: String,
    target_actor_id: String,
    root_id: String,
    authorization_sha256: String,
    deadline_unix_ms: i64,
    operation_timeout_ms: u64,
    ledger_path: PathBuf,
    binding: Binding,
    prompt: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Commit {
    contract: String,
    dispatch_id: String,
    request_sha256: String,
    authorize: bool,
}

fn fail(code: &'static str) -> io::Error {
    io::Error::other(code)
}

fn sha256(bytes: &[u8]) -> String {
    ring::digest::digest(&ring::digest::SHA256, bytes).as_ref().iter().map(|b| format!("{b:02x}")).collect()
}

fn line<T: serde::de::DeserializeOwned>(input: &mut impl BufRead) -> io::Result<T> {
    let mut bytes = Vec::new();
    input.take(MAX_LINE + 1).read_until(b'\n', &mut bytes)?;
    if bytes.len() as u64 > MAX_LINE || !bytes.ends_with(b"\n") {
        return Err(fail("invalid_frame"));
    }
    serde_json::from_slice(&bytes).map_err(|_| fail("invalid_frame"))
}

fn emit(value: &Value) -> io::Result<()> {
    let mut output = io::stdout().lock();
    serde_json::to_writer(&mut output, value).map_err(|_| fail("protocol_unavailable"))?;
    output.write_all(b"\n")?;
    output.flush()
}

/// No symlink/reparse path may redirect the selected workspace, metadata, executable, or private ledger.
fn plain(path: &Path, file: bool) -> io::Result<()> {
    if !path.is_absolute() {
        return Err(fail("invalid_path"));
    }
    for part in path.ancestors() {
        let metadata = match fs::symlink_metadata(part) {
            Ok(m) => m,
            Err(e) if part == path && !file && e.kind() == io::ErrorKind::NotFound => continue,
            Err(_) => return Err(fail("binding_unavailable")),
        };
        #[cfg(windows)]
        let linked = {
            use std::os::windows::fs::MetadataExt;
            metadata.file_attributes() & 0x400 != 0
        };
        #[cfg(not(windows))]
        let linked = metadata.file_type().is_symlink();
        if linked {
            return Err(fail("linked_path"));
        }
        if part == path && file && !metadata.is_file() {
            return Err(fail("invalid_path"));
        }
    }
    Ok(())
}

fn same_directory(left: &Path, right: &Path) -> io::Result<bool> {
    plain(left, false)?;
    plain(right, false)?;
    Ok(left.canonicalize().map_err(|_| fail("binding_unavailable"))?
        == right.canonicalize().map_err(|_| fail("binding_unavailable"))?)
}

fn metadata(path: &Path, first_line: bool) -> io::Result<Value> {
    plain(path, true)?;
    let mut file = BufReader::new(fs::File::open(path)?);
    let mut bytes = Vec::new();
    if first_line {
        file.by_ref().take(MAX_LINE + 1).read_until(b'\n', &mut bytes)?;
    } else {
        file.take(MAX_LINE + 1).read_to_end(&mut bytes)?;
    }
    if bytes.len() as u64 > MAX_LINE {
        return Err(fail("metadata_limit"));
    }
    serde_json::from_slice(&bytes).map_err(|_| fail("invalid_metadata"))
}

fn exact_binding(binding: &Binding) -> io::Result<()> {
    if binding.native_id.is_empty()
        || binding.native_id.len() > 200
        || !same_directory(&binding.workspace, &std::env::current_dir()?)?
    {
        return Err(fail("binding_mismatch"));
    }
    match binding.provider.as_str() {
        "claude-code" => {
            let path = binding.registry_path.as_deref().ok_or_else(|| fail("invalid_binding"))?;
            let pid = binding.pid.filter(|p| *p > 0).ok_or_else(|| fail("invalid_binding"))?;
            if path.file_name().and_then(|s| s.to_str()) != Some(&format!("{pid}.json")) {
                return Err(fail("binding_mismatch"));
            }
            let value = metadata(path, false)?;
            let socket = binding.socket_path.as_deref().ok_or_else(|| fail("invalid_binding"))?;
            let cwd = value["cwd"].as_str().ok_or_else(|| fail("binding_mismatch"))?;
            if value["pid"].as_u64() != Some(pid as u64)
                || !util::pid_alive(pid)
                || value["sessionId"].as_str() != Some(&binding.native_id)
                || value["startedAt"].as_i64() != binding.started_at_ms
                || value["messagingSocketPath"].as_str().map(Path::new) != Some(socket)
                || !same_directory(Path::new(cwd), &binding.workspace)?
            {
                return Err(fail("binding_mismatch"));
            }
            #[cfg(windows)]
            if !socket.to_string_lossy().starts_with(r"\\.\pipe\") {
                return Err(fail("invalid_socket"));
            }
            #[cfg(unix)]
            {
                use std::os::unix::fs::FileTypeExt;
                if !fs::symlink_metadata(socket)?.file_type().is_socket() {
                    return Err(fail("invalid_socket"));
                }
            }
        }
        "codex" => {
            let value = metadata(binding.rollout_path.as_deref().ok_or_else(|| fail("invalid_binding"))?, true)?;
            let cwd = value["payload"]["cwd"].as_str().ok_or_else(|| fail("binding_mismatch"))?;
            if value["type"] != "session_meta"
                || value["payload"]["id"].as_str() != Some(&binding.native_id)
                || !same_directory(Path::new(cwd), &binding.workspace)?
            {
                return Err(fail("binding_mismatch"));
            }
            let path = binding.executable.as_deref().ok_or_else(|| fail("invalid_binding"))?;
            plain(path, true)?;
            #[cfg(windows)]
            if path.extension().and_then(|s| s.to_str()).is_none_or(|s| !s.eq_ignore_ascii_case("exe")) {
                return Err(fail("invalid_executable"));
            }
            let mut context = ring::digest::Context::new(&ring::digest::SHA256);
            let mut file = fs::File::open(path)?;
            let mut buffer = [0; 65536];
            loop {
                let n = file.read(&mut buffer)?;
                if n == 0 {
                    break;
                }
                context.update(&buffer[..n]);
            }
            let digest: String = context.finish().as_ref().iter().map(|b| format!("{b:02x}")).collect();
            if binding.executable_sha256.as_deref() != Some(&digest) {
                return Err(fail("executable_pin_mismatch"));
            }
        }
        _ => return Err(fail("unsupported_provider")),
    }
    Ok(())
}

fn database(path: &Path) -> io::Result<Connection> {
    plain(path, false)?;
    if path.exists() {
        plain(path, true)?;
    }
    let db = Connection::open(path).map_err(|_| fail("ledger_unavailable"))?;
    db.busy_timeout(Duration::from_secs(3)).map_err(|_| fail("ledger_unavailable"))?;
    db.execute_batch("PRAGMA synchronous=FULL; CREATE TABLE IF NOT EXISTS avalokita_reason_attempts (
        dispatch_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, binding_key TEXT NOT NULL, state TEXT NOT NULL, receipt TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS avalokita_reason_queue_receipts (
        dispatch_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, binding_key TEXT NOT NULL, native_id TEXT NOT NULL,
        queued_submission_id TEXT NOT NULL, selected_input_sha256 TEXT NOT NULL, authorization_sha256 TEXT NOT NULL,
        deadline_unix_ms INTEGER NOT NULL, receipt TEXT NOT NULL)")
        .map_err(|_| fail("ledger_unavailable"))?;
    Ok(db)
}

fn prior(request: &Request, hash: &str) -> io::Result<Option<Value>> {
    let db = database(&request.ledger_path)?;
    let row: Option<(String, String, String)> = db
        .query_row(
            "SELECT fingerprint,receipt,binding_key FROM avalokita_reason_attempts WHERE dispatch_id=?1",
            [&request.dispatch_id],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .optional()
        .map_err(|_| fail("ledger_unavailable"))?;
    row.map(|(fingerprint, text, binding_key)| {
        if fingerprint != hash {
            return Err(fail("dispatch_identity_reused"));
        }
        let mut receipt: Value = serde_json::from_str(&text).map_err(|_| fail("ledger_unavailable"))?;
        let queue: Option<(String, String, String)> = db
            .query_row(
                "SELECT fingerprint,binding_key,receipt FROM avalokita_reason_queue_receipts WHERE dispatch_id=?1",
                [&request.dispatch_id],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )
            .optional()
            .map_err(|_| fail("ledger_unavailable"))?;
        if let Some((queue_fingerprint, queue_binding_key, text)) = queue {
            if queue_fingerprint != hash || queue_binding_key != binding_key || request.binding.provider != "codex" {
                return Err(fail("native_queue_receipt_conflict"));
            }
            receipt["queue_receipt"] = serde_json::from_str(&text).map_err(|_| fail("ledger_unavailable"))?;
        }
        if matches!(receipt["state"].as_str(), Some("native_input_verified" | "native_result_verified")) {
            // Authenticated observations belong to the adapter's monotonic state, not our wire authority.
            let accepted = if request.binding.provider == "codex" {
                receipt.get("queue_receipt").is_some()
            } else {
                receipt["transport_accepted"] == true
            };
            receipt["state"] = json!(if accepted { "transport_accepted" } else { "delivery_unknown" });
            receipt["transport_accepted"] = json!(accepted);
            receipt["native_accepted"] = json!(false);
            receipt["native_result_verified"] = json!(false);
            receipt.as_object_mut().ok_or_else(|| fail("ledger_unavailable"))?.remove("native_observation");
            if receipt.get("queue_receipt").is_some() {
                receipt["transport"] = json!("codex_queue_accepted");
            }
        }
        receipt["duplicate_suppressed"] = json!(true);
        Ok(receipt)
    })
    .transpose()
}

fn save(request: &Request, hash: &str, receipt: &Value, first: bool) -> io::Result<()> {
    let mut db = database(&request.ledger_path)?;
    let tx = db
        .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
        .map_err(|_| fail("ledger_unavailable"))?;
    let binding_key = sha256(
        &serde_json::to_vec(&(
            &request.binding.provider,
            &request.binding.native_id,
            request.binding.workspace.canonicalize().map_err(|_| fail("binding_unavailable"))?,
        ))
        .map_err(|_| fail("invalid_binding"))?,
    );
    if first
        && tx
            .query_row(
                "SELECT 1 FROM avalokita_reason_attempts WHERE binding_key=?1 AND state='delivery_unknown'",
                [&binding_key],
                |_| Ok(()),
            )
            .optional()
            .map_err(|_| fail("ledger_unavailable"))?
            .is_some()
    {
        return Err(fail("target_delivery_unknown"));
    }
    if let Some(queue) = receipt.get("queue_receipt") {
        if first
            || request.binding.provider != "codex"
            || queue["contract"] != "avalokita.codex-queue/1"
            || queue["native_id"] != request.binding.native_id
            || queue["engine_request_sha256"] != hash
            || queue["selected_input_sha256"] != sha256(request.prompt.as_bytes())
            || queue["authorization_sha256"] != request.authorization_sha256
            || queue["deadline_unix_ms"] != request.deadline_unix_ms
            || queue["executable_sha256"].as_str() != request.binding.executable_sha256.as_deref()
        {
            return Err(fail("native_queue_receipt_conflict"));
        }
        let attempt: Option<(String, String)> = tx
            .query_row(
                "SELECT fingerprint,binding_key FROM avalokita_reason_attempts WHERE dispatch_id=?1",
                [&request.dispatch_id],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .optional()
            .map_err(|_| fail("ledger_unavailable"))?;
        if attempt.as_ref().is_none_or(|(fingerprint, key)| fingerprint != hash || key != &binding_key) {
            return Err(fail("native_queue_receipt_conflict"));
        }
        let previous: Option<(String, String, String)> = tx
            .query_row(
                "SELECT fingerprint,binding_key,receipt FROM avalokita_reason_queue_receipts WHERE dispatch_id=?1",
                [&request.dispatch_id],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )
            .optional()
            .map_err(|_| fail("ledger_unavailable"))?;
        if let Some((fingerprint, key, text)) = previous {
            let saved: Value = serde_json::from_str(&text).map_err(|_| fail("ledger_unavailable"))?;
            if fingerprint != hash || key != binding_key || saved != *queue {
                return Err(fail("native_queue_receipt_conflict"));
            }
        } else {
            // Queue identity is immutable and separate from a callback's monotonic attempt receipt.
            // The Python observation trigger may restore that receipt wholesale after this commit.
            tx.execute(
                "INSERT INTO avalokita_reason_queue_receipts VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9)",
                params![
                    request.dispatch_id,
                    hash,
                    binding_key,
                    request.binding.native_id,
                    queue["queued_submission_id"].as_str(),
                    queue["selected_input_sha256"].as_str(),
                    request.authorization_sha256,
                    request.deadline_unix_ms,
                    queue.to_string()
                ],
            )
            .map_err(|_| fail("ledger_unavailable"))?;
        }
    }
    let changed = if first {
        tx.execute(
            "INSERT OR IGNORE INTO avalokita_reason_attempts VALUES (?1,?2,?3,?4,?5)",
            params![request.dispatch_id, hash, binding_key, receipt["state"].as_str(), receipt.to_string()],
        )
    } else {
        tx.execute(
            "UPDATE avalokita_reason_attempts SET state=?3,receipt=?4 WHERE dispatch_id=?1 AND fingerprint=?2",
            params![request.dispatch_id, hash, receipt["state"].as_str(), receipt.to_string()],
        )
    }
    .map_err(|_| fail("ledger_unavailable"))?;
    if changed != 1 {
        return Err(fail("dispatch_already_attempted"));
    }
    tx.commit().map_err(|_| fail("ledger_unavailable"))?;
    Ok(())
}

fn receipt(request: &Request) -> Value {
    json!({"contract": CONTRACT, "type": "receipt", "dispatch_id": request.dispatch_id,
        "root_id": request.root_id, "state": "not_sent", "native_input_attempted": false,
        "transport_accepted": false, "native_accepted": false, "native_result_verified": false,
        "automatic_retry": false, "fallback_attempted": false})
}

fn before_input(request: &Request, hash: &str, input: &mut impl BufRead, result: &mut Value) -> io::Result<()> {
    emit(&json!({"contract": CONTRACT, "type": "ready", "dispatch_id": request.dispatch_id,
        "root_id": request.root_id, "target_actor_id": request.target_actor_id,
        "native_id": request.binding.native_id, "request_sha256": hash}))?;
    let commit: Commit = line(input)?;
    if commit.contract != CONTRACT
        || commit.dispatch_id != request.dispatch_id
        || commit.request_sha256 != hash
        || !commit.authorize
        || util::now_ms() >= request.deadline_unix_ms
    {
        return Err(fail("authorization_revoked"));
    }
    exact_binding(&request.binding)?;
    if util::now_ms() >= request.deadline_unix_ms {
        return Err(fail("authorization_revoked"));
    }
    // This FULL-synchronous commit precedes the first native user frame or queue process spawn.
    let attempted = json!({"state": "delivery_unknown", "native_input_attempted": true});
    let mut next = result.clone();
    next.as_object_mut().unwrap().extend(attempted.as_object().unwrap().clone());
    if let Err(error) = save(request, hash, &next, true) {
        if error.to_string() == "dispatch_already_attempted" {
            if let Some(saved) = prior(request, hash)? {
                *result = saved;
            }
        }
        return Err(error);
    }
    *result = next;
    if util::now_ms() >= request.deadline_unix_ms {
        return Err(fail("authorization_revoked"));
    }
    Ok(())
}

fn claude_token(binding: &Binding) -> io::Result<Option<String>> {
    if !cfg!(windows) {
        return Ok(None);
    }
    let parent = binding.registry_path.as_deref().and_then(Path::parent).ok_or_else(|| fail("invalid_binding"))?;
    let prefix = format!("{}.", binding.pid.ok_or_else(|| fail("invalid_binding"))?);
    let mut token = None;
    for entry in fs::read_dir(parent)? {
        let entry = entry?;
        let name = entry.file_name().to_string_lossy().to_string();
        if name.starts_with(&prefix) && name.ends_with(".key") {
            if token.is_some() {
                return Err(fail("ambiguous_native_token"));
            }
            let value = metadata(&entry.path(), false)?;
            token = Some(
                value["peerToken"]
                    .as_str()
                    .filter(|s| !s.is_empty())
                    .ok_or_else(|| fail("native_token_unavailable"))?
                    .to_owned(),
            );
        }
    }
    token.ok_or_else(|| fail("native_token_unavailable")).map(Some)
}

/// Microsoft CRT quoting used by Command: spaces/tabs quote an argument, quotes
/// escape preceding backslashes, and trailing backslashes double before a quote.
#[cfg(windows)]
fn codex_command_line_units(binding: &Binding, prompt: &str) -> io::Result<usize> {
    let exe = binding.executable.as_deref().and_then(Path::to_str).ok_or_else(|| fail("invalid_executable"))?;
    let args = ["queue", "--thread", &binding.native_id, "--message", prompt];
    if exe.contains('\0') || args.iter().any(|arg| arg.contains('\0')) {
        return Err(fail("invalid_native_argument"));
    }
    // Rust always quotes argv[0]. The final NUL counts toward CreateProcessW's limit.
    let mut total = 2 + exe.encode_utf16().count() + 1;
    for arg in args {
        let quoted = arg.is_empty() || arg.contains([' ', '\t']);
        let mut units = if quoted { 2 } else { 0 };
        let mut slashes = 0;
        for unit in arg.encode_utf16() {
            if unit == b'\\' as u16 {
                slashes += 1;
            } else if unit == b'"' as u16 {
                units += 2 * slashes + 2;
                slashes = 0;
            } else {
                units += slashes + 1;
                slashes = 0;
            }
        }
        units += slashes * if quoted { 2 } else { 1 };
        total += 1 + units;
    }
    Ok(total)
}

fn codex_queue_id<'a>(output: &'a [u8], native_id: &str) -> io::Result<&'a str> {
    if output.len() as u64 > MAX_QUEUE_STDOUT {
        return Err(fail("native_queue_output_limit"));
    }
    let text = std::str::from_utf8(output).map_err(|_| fail("native_queue_receipt_invalid"))?;
    let line = text
        .strip_suffix("\r\n")
        .or_else(|| text.strip_suffix('\n'))
        .ok_or_else(|| fail("native_queue_receipt_invalid"))?;
    if line.chars().any(char::is_control) {
        return Err(fail("native_queue_receipt_invalid"));
    }
    // Codex 51f97c6bca55: session_queue_commands.rs:78-81, then println in cli/main.rs.
    // Anchor the whole output to the selected thread; do not search or trim native output.
    let suffix = format!(" for thread {native_id}.");
    let id = line
        .strip_prefix("Queued message ")
        .and_then(|value| value.strip_suffix(&suffix))
        .ok_or_else(|| fail("native_queue_receipt_invalid"))?;
    if id.is_empty() || id.len() > 512 || id.chars().any(|c| c.is_whitespace() || c.is_control()) {
        return Err(fail("native_queue_receipt_invalid"));
    }
    Ok(id)
}

async fn deliver(request: &Request, input: &mut impl BufRead, result: &mut Value) -> io::Result<()> {
    if request.contract != CONTRACT
        || request.prompt.is_empty()
        || request.prompt.len() > 65536
        || [&request.dispatch_id, &request.source_actor_id, &request.target_actor_id, &request.root_id]
            .iter()
            .any(|s| s.is_empty() || s.len() > 200)
        || util::now_ms() >= request.deadline_unix_ms
        || request.authorization_sha256.len() != 64
        || !request.authorization_sha256.bytes().all(|b| b.is_ascii_hexdigit())
        || request.operation_timeout_ms == 0
        || request.operation_timeout_ms > 60000
    {
        return Err(fail("invalid_request"));
    }
    exact_binding(&request.binding)?;
    let hash = sha256(&serde_json::to_vec(request).map_err(|_| fail("invalid_request"))?);
    if let Some(saved) = prior(request, &hash)? {
        *result = saved;
        return Ok(());
    }
    match request.binding.provider.as_str() {
        "claude-code" => {
            let socket = agent::ClaudeSocket {
                path: request.binding.socket_path.clone().unwrap(),
                token: claude_token(&request.binding)?,
            };
            agent::post_to_claude_guarded(&socket, &request.prompt, || before_input(request, &hash, input, result))?;
            result["transport"] = json!("claude_socket_written");
        }
        "codex" => {
            #[cfg(windows)]
            if codex_command_line_units(&request.binding, &request.prompt)? > 32767 {
                return Err(fail("native_command_line_limit"));
            }
            before_input(request, &hash, input, result)?;
            let mut command = tokio::process::Command::new(request.binding.executable.as_deref().unwrap());
            command
                .args(["queue", "--thread", &request.binding.native_id, "--message", &request.prompt])
                .current_dir(&request.binding.workspace)
                .stdin(Stdio::null())
                .stdout(Stdio::piped())
                .stderr(Stdio::null())
                .kill_on_drop(true);
            let mut child = command.spawn().map_err(|_| fail("native_spawn_unavailable"))?;
            // Leave time for the parent to read our receipt after we have killed/reaped a timed-out queue helper.
            let milliseconds = (request.deadline_unix_ms - util::now_ms()).clamp(1, 30000) as u64;
            let milliseconds = milliseconds.min(request.operation_timeout_ms.saturating_sub(2000).max(1));
            let completed = tokio::time::timeout(Duration::from_millis(milliseconds), async {
                let stdout = child.stdout.take().ok_or_else(|| fail("native_queue_uncertain"))?;
                let mut bounded = tokio::io::AsyncReadExt::take(stdout, MAX_QUEUE_STDOUT + 1);
                let mut output = Vec::new();
                tokio::io::AsyncReadExt::read_to_end(&mut bounded, &mut output)
                    .await
                    .map_err(|_| fail("native_queue_uncertain"))?;
                if output.len() as u64 > MAX_QUEUE_STDOUT {
                    return Err(fail("native_queue_output_limit"));
                }
                if !child.wait().await.map_err(|_| fail("native_queue_uncertain"))?.success() {
                    return Err(fail("native_queue_uncertain"));
                }
                Ok(output)
            })
            .await;
            let output = match completed {
                Ok(Ok(output)) => output,
                Ok(Err(error)) => {
                    let _ = child.kill().await;
                    return Err(error);
                }
                Err(_) => {
                    let _ = child.kill().await;
                    return Err(fail("native_timeout"));
                }
            };
            let queue_id = codex_queue_id(&output, &request.binding.native_id)?;
            result["queue_receipt"] = json!({"contract": "avalokita.codex-queue/1",
                "queued_submission_id": queue_id, "native_id": request.binding.native_id,
                "selected_input_sha256": sha256(request.prompt.as_bytes()), "engine_request_sha256": hash,
                "authorization_sha256": request.authorization_sha256, "deadline_unix_ms": request.deadline_unix_ms,
                "executable_sha256": request.binding.executable_sha256, "stdout_sha256": sha256(&output)});
            result["transport"] = json!("codex_queue_accepted");
        }
        _ => return Err(fail("unsupported_provider")),
    }
    result["state"] = json!("transport_accepted");
    result["transport_accepted"] = json!(true);
    save(request, &hash, result, false)
}

pub async fn run() -> Result<()> {
    let mut input = BufReader::new(io::stdin());
    let request: Request = match line(&mut input) {
        Ok(value) => value,
        Err(_) => {
            emit(
                &json!({"contract": CONTRACT, "type": "receipt", "state": "not_sent", "reason_code": "invalid_frame"}),
            )?;
            return Ok(());
        }
    };
    let mut result = receipt(&request);
    if let Err(error) = deliver(&request, &mut input, &mut result).await {
        // Do not leak native stderr, paths, auth tokens, or the prompt into receipts.
        let code = error.to_string();
        result["reason_code"] = json!(if [
            "binding_mismatch",
            "authorization_revoked",
            "executable_pin_mismatch",
            "dispatch_identity_reused",
            "dispatch_already_attempted",
            "target_delivery_unknown",
            "native_timeout",
            "native_queue_uncertain",
            "native_queue_receipt_invalid",
            "native_queue_output_limit",
            "native_queue_receipt_conflict",
            "native_token_unavailable",
            "ambiguous_native_token",
            "native_spawn_unavailable",
            "native_command_line_limit",
            "invalid_native_argument"
        ]
        .contains(&code.as_str())
        {
            code.as_str()
        } else {
            "native_unavailable_or_uncertain"
        });
        if result["native_input_attempted"] == true {
            result["state"] = json!("delivery_unknown");
            result["transport_accepted"] = json!(false);
            if let Ok(bytes) = serde_json::to_vec(&request) {
                let _ = save(&request, &sha256(&bytes), &result, false);
            }
        }
    }
    emit(&result)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn queue_receipt_requires_one_exact_bounded_line_and_preserves_opaque_ids() {
        for id in ["opaque:queue_01/+==", "not-a-uuid", "수신항목:1"] {
            for ending in ["\n", "\r\n"] {
                let output = format!("Queued message {id} for thread exact-thread.{ending}");
                assert_eq!(codex_queue_id(output.as_bytes(), "exact-thread").unwrap(), id);
            }
        }
        for output in [
            b"".as_slice(),
            b"Queued message q for thread different-thread.\n",
            b"Queued message q for thread exact-thread\n",
            b"Queued message q for thread exact-thread.",
            b"Queued message  for thread exact-thread.\n",
            b"Queued message q q for thread exact-thread.\n",
            b"Queued message q\0 for thread exact-thread.\n",
            b"Queued message q\x1b for thread exact-thread.\n",
            b"Queued message \xff for thread exact-thread.\n",
            b"log\nQueued message q for thread exact-thread.\n",
            b"Queued message q for thread exact-thread.\n\n",
            b"Queued message q for thread exact-thread.\nQueued message q for thread exact-thread.\n",
        ] {
            assert!(codex_queue_id(output, "exact-thread").is_err(), "{output:?}");
        }
        assert!(
            codex_queue_id(
                format!("Queued message {} for thread exact-thread.\n", "q".repeat(513)).as_bytes(),
                "exact-thread"
            )
            .is_err()
        );
        assert!(codex_queue_id(&vec![b'q'; MAX_QUEUE_STDOUT as usize + 1], "exact-thread").is_err());
    }

    struct Fixture {
        directory: PathBuf,
        request: Request,
    }

    impl Fixture {
        fn new() -> Self {
            let directory = std::env::temp_dir().join(format!("avalokita-reason-{}", ulid::Ulid::generate()));
            fs::create_dir(&directory).unwrap();
            let workspace = std::env::current_dir().unwrap();
            let executable = directory.join("synthetic-codex.exe");
            fs::write(&executable, b"not an executable; metadata tests never spawn it").unwrap();
            let rollout = directory.join("selected.jsonl");
            fs::write(
                &rollout,
                format!("{}\n", json!({"type":"session_meta", "payload":{"id":"exact-thread", "cwd":workspace}})),
            )
            .unwrap();
            Self {
                request: Request {
                    contract: CONTRACT.into(),
                    dispatch_id: "dispatch".into(),
                    source_actor_id: "claude-ceo".into(),
                    target_actor_id: "codex-design".into(),
                    root_id: "human-root".into(),
                    authorization_sha256: "a".repeat(64),
                    deadline_unix_ms: util::now_ms() + 60000,
                    operation_timeout_ms: 30000,
                    ledger_path: directory.join("private.sqlite3"),
                    prompt: "selected public task".into(),
                    binding: Binding {
                        provider: "codex".into(),
                        native_id: "exact-thread".into(),
                        workspace,
                        registry_path: None,
                        pid: None,
                        started_at_ms: None,
                        socket_path: None,
                        rollout_path: Some(rollout),
                        executable: Some(executable),
                        executable_sha256: Some(sha256(b"not an executable; metadata tests never spawn it")),
                    },
                },
                directory,
            }
        }
        fn hash(&self) -> String {
            sha256(&serde_json::to_vec(&self.request).unwrap())
        }
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            // Every file was created by this fixture. No recursive deletion or native state paths.
            for name in ["selected.jsonl", "synthetic-codex.exe", "private.sqlite3", "private.sqlite3-journal"] {
                let _ = fs::remove_file(self.directory.join(name));
            }
            let _ = fs::remove_dir(&self.directory);
        }
    }

    #[test]
    fn exact_metadata_rejects_a_different_thread_and_executable_without_a_native_call() {
        let fixture = Fixture::new();
        exact_binding(&fixture.request.binding).unwrap();
        let mut other = fixture.request.binding.clone();
        other.native_id = "another-thread".into();
        assert!(exact_binding(&other).is_err());
        let mut other = fixture.request.binding.clone();
        other.executable_sha256 = Some("0".repeat(64));
        assert!(exact_binding(&other).is_err());
    }

    #[test]
    fn identical_thread_metadata_from_another_workspace_is_not_attached() {
        let fixture = Fixture::new();
        fs::write(
            fixture.request.binding.rollout_path.as_ref().unwrap(),
            format!("{}\n", json!({"type":"session_meta", "payload":{"id":"exact-thread", "cwd":fixture.directory}})),
        )
        .unwrap();
        assert!(exact_binding(&fixture.request.binding).is_err());
    }

    #[test]
    fn a_durable_unknown_survives_reopen_and_cannot_be_reserved_under_a_fresh_root() {
        let mut fixture = Fixture::new();
        let mut unknown = receipt(&fixture.request);
        unknown["state"] = json!("delivery_unknown");
        unknown["native_input_attempted"] = json!(true);
        save(&fixture.request, &fixture.hash(), &unknown, true).unwrap();
        assert_eq!(prior(&fixture.request, &fixture.hash()).unwrap().unwrap()["state"], "delivery_unknown");
        fixture.request.root_id = "fresh-human-root".into();
        fixture.request.dispatch_id = "fresh-dispatch".into();
        assert_eq!(
            save(&fixture.request, &fixture.hash(), &unknown, true).unwrap_err().to_string(),
            "target_delivery_unknown"
        );
    }

    #[test]
    fn changed_prompt_or_causal_authority_cannot_reuse_a_dispatch() {
        let mut fixture = Fixture::new();
        let initial_hash = fixture.hash();
        save(&fixture.request, &initial_hash, &receipt(&fixture.request), true).unwrap();
        fixture.request.prompt = "different work".into();
        assert!(prior(&fixture.request, &fixture.hash()).is_err());
        fixture.request.prompt = "selected public task".into();
        fixture.request.authorization_sha256 = "b".repeat(64);
        assert!(prior(&fixture.request, &fixture.hash()).is_err());
    }

    #[test]
    fn immutable_queue_facts_survive_an_earlier_native_observation_and_reopen() {
        let fixture = Fixture::new();
        let hash = fixture.hash();
        let mut result = receipt(&fixture.request);
        result["state"] = json!("delivery_unknown");
        result["native_input_attempted"] = json!(true);
        save(&fixture.request, &hash, &result, true).unwrap();
        let db = database(&fixture.request.ledger_path).unwrap();
        // Same production trigger as the frozen Python adapter; no actual native callback.
        db.execute_batch("CREATE TRIGGER avalokita_reason_observation_monotonic
            AFTER UPDATE ON avalokita_reason_attempts
            WHEN (OLD.state='native_result_verified' AND NEW.state!='native_result_verified')
                OR (OLD.state='native_input_verified' AND NEW.state NOT IN ('native_input_verified','native_result_verified'))
            BEGIN UPDATE avalokita_reason_attempts SET state=OLD.state,receipt=OLD.receipt
                WHERE dispatch_id=OLD.dispatch_id; END").unwrap();
        let mut observed = result.clone();
        observed["state"] = json!("native_result_verified");
        observed["native_accepted"] = json!(true);
        observed["native_result_verified"] = json!(true);
        observed["native_observation"] = json!({"kind":"result_completed"});
        db.execute(
            "UPDATE avalokita_reason_attempts SET state=?1,receipt=?2 WHERE dispatch_id=?3",
            params!["native_result_verified", observed.to_string(), fixture.request.dispatch_id],
        )
        .unwrap();
        drop(db);
        result["state"] = json!("transport_accepted");
        result["transport_accepted"] = json!(true);
        result["queue_receipt"] = json!({"contract":"avalokita.codex-queue/1", "queued_submission_id":"opaque:queue/1",
            "native_id":fixture.request.binding.native_id, "selected_input_sha256":sha256(fixture.request.prompt.as_bytes()),
            "engine_request_sha256":hash, "authorization_sha256":fixture.request.authorization_sha256,
            "deadline_unix_ms":fixture.request.deadline_unix_ms, "executable_sha256":fixture.request.binding.executable_sha256,
            "stdout_sha256":sha256(b"synthetic stdout")});
        save(&fixture.request, &hash, &result, false).unwrap();
        save(&fixture.request, &hash, &result, false).unwrap();
        let reopened = prior(&fixture.request, &hash).unwrap().unwrap();
        assert_eq!(reopened["state"], "transport_accepted");
        assert_eq!(reopened["native_result_verified"], false);
        assert!(reopened.get("native_observation").is_none());
        let db = database(&fixture.request.ledger_path).unwrap();
        let stored: String = db.query_row("SELECT state FROM avalokita_reason_attempts", [], |r| r.get(0)).unwrap();
        assert_eq!(stored, "native_result_verified");
        drop(db);
        assert_eq!(reopened["queue_receipt"], result["queue_receipt"]);
        result["queue_receipt"]["queued_submission_id"] = json!("different-queue");
        assert_eq!(
            save(&fixture.request, &hash, &result, false).unwrap_err().to_string(),
            "native_queue_receipt_conflict"
        );
        assert_eq!(prior(&fixture.request, &hash).unwrap().unwrap()["queue_receipt"], reopened["queue_receipt"]);
    }

    #[test]
    fn a_native_observation_without_an_exact_queue_row_is_not_transport_wire_authority() {
        let fixture = Fixture::new();
        let hash = fixture.hash();
        let mut observed = receipt(&fixture.request);
        observed["state"] = json!("native_result_verified");
        observed["native_input_attempted"] = json!(true);
        observed["transport_accepted"] = json!(true);
        observed["native_accepted"] = json!(true);
        observed["native_result_verified"] = json!(true);
        observed["native_observation"] = json!({"kind":"result_completed"});
        save(&fixture.request, &hash, &observed, true).unwrap();
        let wire = prior(&fixture.request, &hash).unwrap().unwrap();
        assert_eq!(wire["state"], "delivery_unknown");
        assert_eq!(wire["transport_accepted"], false);
        assert_eq!(wire["native_accepted"], false);
        assert_eq!(wire["native_result_verified"], false);
        assert!(wire.get("native_observation").is_none());
    }

    #[test]
    fn a_delayed_durable_commit_cannot_cross_the_native_input_deadline() {
        let mut fixture = Fixture::new();
        let db = database(&fixture.request.ledger_path).unwrap();
        drop(db);
        let path = fixture.request.ledger_path.clone();
        let (locked, acquired) = std::sync::mpsc::channel();
        let holder = std::thread::spawn(move || {
            let db = Connection::open(path).unwrap();
            db.execute_batch("BEGIN EXCLUSIVE").unwrap();
            locked.send(()).unwrap();
            std::thread::sleep(Duration::from_millis(300));
            db.execute_batch("COMMIT").unwrap();
        });
        acquired.recv().unwrap();
        fixture.request.deadline_unix_ms = util::now_ms() + 100;
        let hash = fixture.hash();
        let commit = json!({"contract":CONTRACT, "dispatch_id":fixture.request.dispatch_id,
            "request_sha256":hash, "authorize":true});
        let mut input = io::Cursor::new(format!("{commit}\n"));
        let mut result = receipt(&fixture.request);
        let error = before_input(&fixture.request, &hash, &mut input, &mut result).unwrap_err();
        assert_eq!(error.to_string(), "authorization_revoked");
        holder.join().unwrap();
        // The caller must propagate this error instead of writing a user frame or spawning queue.
        // The conservative attempt marker still prevents a crash/restart from replaying the input.
        assert_eq!(prior(&fixture.request, &hash).unwrap().unwrap()["state"], "delivery_unknown");
    }

    #[cfg(windows)]
    #[tokio::test]
    async fn impossible_windows_arguments_are_rejected_before_native_attempt_or_ready() {
        let mut fixture = Fixture::new();
        for (prompt, code) in
            [("x".repeat(40000), "native_command_line_limit"), ("x\0y".into(), "invalid_native_argument")]
        {
            fixture.request.prompt = prompt;
            let mut input = io::Cursor::new(Vec::<u8>::new());
            let mut result = receipt(&fixture.request);
            assert_eq!(deliver(&fixture.request, &mut input, &mut result).await.unwrap_err().to_string(), code);
            assert_eq!(result["state"], "not_sent");
            assert_eq!(result["native_input_attempted"], false);
            assert!(prior(&fixture.request, &fixture.hash()).unwrap().is_none());
        }
    }
}
