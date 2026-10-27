// A windowless OpenSSH AskPass bridge. Secrets travel only over a local socket
// and the stdout pipe inherited from ssh.exe, never through command arguments.
using System;
using System.IO;
using System.Net.Sockets;
using System.Text;

class AskPass {
    static void Send(BinaryWriter writer, string text) {
        byte[] bytes = Encoding.UTF8.GetBytes(text ?? "");
        writer.Write(bytes.Length);
        writer.Write(bytes);
        writer.Flush();
    }
    static int Main(string[] args) {
        try {
            using (TcpClient client = new TcpClient()) {
                client.Connect("127.0.0.1", int.Parse(Environment.GetEnvironmentVariable("TUNNEL_ASKPASS_PORT")));
                using (NetworkStream stream = client.GetStream())
                using (BinaryWriter writer = new BinaryWriter(stream))
                using (BinaryReader reader = new BinaryReader(stream)) {
                    Send(writer, Environment.GetEnvironmentVariable("TUNNEL_ASKPASS_TOKEN"));
                    Send(writer, args.Length > 0 ? args[0] : "");
                    Send(writer, Environment.GetEnvironmentVariable("SSH_ASKPASS_PROMPT"));
                    if (reader.ReadByte() != 1) return 1;
                    int length = reader.ReadInt32();
                    if (length < 0 || length > 65536) return 1;
                    byte[] answer = reader.ReadBytes(length);
                    if (answer.Length != length) return 1;
                    using (Stream output = Console.OpenStandardOutput()) {
                        output.Write(answer, 0, answer.Length);
                        output.WriteByte(10);
                        output.Flush();
                    }
                    Array.Clear(answer, 0, answer.Length);
                    return 0;
                }
            }
        } catch { return 1; } // Never print prompts, credentials or exceptions.
    }
}
