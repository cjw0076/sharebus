#!/usr/bin/env node
/**
 * Adapted from Happy Coder's Session Hook Forwarder (MIT).
 * Copyright (c) 2026 Happy Coder Contributors.
 * Pinned source, license and exact modifications: ../../third_party/NOTICE.md.
 *
 * Sharebus bounds stdin and forwards selected lifecycle fields to its local
 * Python helper with an argument vector, without a shell or raw-payload log.
 */
'use strict';
const { spawn } = require('node:child_process');
const executable = process.argv[2];
const args = process.argv.slice(3);
if (!executable || args.length === 0) process.exit(1);
if (process.env.SHAREBUS_MANAGED || process.env.SHAREBUS_AGENT_ID || process.env.SHAREBUS_AGENT_TOKEN_FILE) process.exit(0);
const chunks = [];
let size = 0;
process.stdin.on('data', (chunk) => {
  size += chunk.length;
  if (size > 1024 * 1024) process.exit(1);
  chunks.push(chunk);
});
process.stdin.on('end', () => {
  let payload;
  try {
    payload = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    if (!payload || Array.isArray(payload) || typeof payload !== 'object') process.exit(1);
  } catch { process.exit(1); }
  const selected = {};
  for (const key of ['session_id', 'cwd', 'hook_event_name', 'turn_id', 'stop_hook_active', 'cancelled', 'interrupted', 'aborted',
    'conversationId', 'workspacePaths', 'transcriptPath', 'modelName', 'invocationNum',
    'executionNum', 'fullyIdle', 'prompt', 'last_assistant_message']) {
    if (Object.hasOwn(payload, key)) selected[key] = payload[key];
  }
  const child = spawn(executable, args, { shell: false, windowsHide: true, stdio: ['pipe', 'inherit', 'inherit'] });
  const timer = setTimeout(() => { child.kill(); }, 8000);
  child.on('error', () => { clearTimeout(timer); process.exitCode = 1; });
  child.on('exit', (code) => { clearTimeout(timer); process.exitCode = code ?? 1; });
  child.stdin.on('error', () => {});
  child.stdin.end(Buffer.from(JSON.stringify(selected), 'utf8'));
});
process.stdin.resume();
