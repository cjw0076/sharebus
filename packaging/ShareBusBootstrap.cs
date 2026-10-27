// MIT. Windowless portable entry; no shell, account access or model input.
using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

internal static class ShareBusBootstrap
{
    [STAThread]
    private static int Main()
    {
        string root = AppDomain.CurrentDomain.BaseDirectory;
        string python = Path.Combine(root, "python", "pythonw.exe");
        string script = Path.Combine(root, "sharebus", "launcher.py");
        if (!File.Exists(python) || !File.Exists(script))
        {
            MessageBox.Show("ShareBus 파일이 부족합니다. 압축을 모두 푼 뒤 다시 열어 주세요.", "ShareBus");
            return 1;
        }
        var start = new ProcessStartInfo(python, "-I -B \"" + script + "\"");
        start.UseShellExecute = false;
        start.CreateNoWindow = true;
        start.WorkingDirectory = root;
        start.EnvironmentVariables.Remove("PYTHONPATH");
        start.EnvironmentVariables.Remove("PYTHONHOME");
        try
        {
            Process child = Process.Start(start);
            return child == null ? 1 : 0;
        }
        catch (Exception)
        {
            MessageBox.Show("ShareBus를 열지 못했습니다. 파일을 다시 확인해 주세요.", "ShareBus");
            return 1;
        }
    }
}
