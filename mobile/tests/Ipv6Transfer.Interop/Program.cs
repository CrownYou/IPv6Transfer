using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using Ipv6Transfer.Core.Protocol;

namespace Ipv6Transfer.Interop;

/// <summary>
/// 协议互通测试工具：既能让 C# 当服务器 / 客户端跑单机自测，
/// 也能和 python 版 file_core.py 互相对传，验证协议完全一致。
/// </summary>
internal static class Program
{
    private const string AppName = "interop/1.0";

    private static int Main(string[] args)
    {
        Console.OutputEncoding = new UTF8Encoding(false);
        try
        {
            return Run(args);
        }
        catch (Exception ex)
        {
            Console.WriteLine($"FATAL {ex.GetType().Name}: {ex.Message}");
            return 3;
        }
    }

    private static int Run(string[] args)
    {
        // 用 "-" 表示空密码（命令行里不好传空字符串）
        args = Array.ConvertAll(args, arg => arg == "-" ? string.Empty : arg);
        if (args.Length == 0)
        {
            Console.WriteLine(Help);
            return 2;
        }

        var command = args[0];
        switch (command)
        {
            case "cfg-encode":
                Console.WriteLine(ConnectionConfigCodec.Encode(args[1], int.Parse(args[2]), args.Length > 3 ? args[3] : ""));
                return 0;
            case "cfg-decode":
            {
                var info = ConnectionConfigCodec.Decode(args[1]);
                Console.WriteLine($"host={info.Host} port={info.Port} password={info.Password}");
                return 0;
            }
            case "server":
                return Serve(args[1], args[2], int.Parse(args[3]), args.Length > 4 ? args[4] : "", args.Length > 5 ? int.Parse(args[5]) : 1);
            case "send":
                return Send(args[1], int.Parse(args[2]), args[3], args.Length > 4 ? args[4] : "");
            case "recv":
                return Recv(args[1], int.Parse(args[2]), args[3], args[4], args.Length > 5 ? args[5] : "");
            case "hold4":
                return Hold4(args[1], int.Parse(args[2]), args.Length > 3 ? int.Parse(args[3]) : 30, args.Length > 4 ? args[4] : "");
            case "dual":
                return Dual(args[1], args[2], int.Parse(args[3]), args.Length > 4 ? args[4] : "");
            case "ctl4":
                return ControlSendIpv4(args[1], int.Parse(args[2]), args[3], args.Length > 4 ? args[4] : "");
            case "send4":
                return SendIpv4(args[1], int.Parse(args[2]), args[3], args.Length > 4 ? args[4] : "");
            case "recv4":
                return RecvIpv4(args[1], int.Parse(args[2]), args[3], args[4], args.Length > 5 ? args[5] : "");
            case "control-serve":
                return ControlServe(args[1], int.Parse(args[2]), args.Length > 3 ? args[3] : "");
            case "control-send":
                return ControlSend(args[1], int.Parse(args[2]), args[3], args.Length > 3 ? args[4] : "");
            case "push-serve":
                return PushServe(args[1], args[2], int.Parse(args[3]), args[4], args.Length > 5 ? args[5] : "");
            case "push-recv":
                return PushRecv(args[1], int.Parse(args[2]), args[3], args.Length > 4 ? args[4] : "");
            case "selftest":
                return SelfTest(args.Length > 1 ? args[1] : Path.Combine(Path.GetTempPath(), "ipv6interop"));
            case "addresses":
                foreach (var entry in Ipv6Addresses.List())
                {
                    Console.WriteLine(Ipv6Addresses.FormatEntry(entry));
                }

                return 0;
            default:
                Console.WriteLine($"未知命令: {command}");
                return 2;
        }
    }

    private const string Help = "cfg-encode|cfg-decode|server|send|recv|control-serve|control-send|push-serve|push-recv|selftest|addresses";

    // ------------------------------------------------------------------ #
    // 服务器
    // ------------------------------------------------------------------ #

    private static int Serve(string dir, string host, int port, string password, int count)
    {
        Directory.CreateDirectory(dir);
        using var listener = Handshake.MakeListener(host, port);
        Console.WriteLine($"READY port={port}");
        var locks = new NameLocks();
        var handled = 0;
        while (handled < count)
        {
            var client = listener.Accept();
            var peer = PeerFormatter.Format(client.RemoteEndPoint);
            var conn = new FramedConnection(client);
            var keepSocket = false;
            try
            {
                var hello = Handshake.ReadHello(conn);
                Handshake.Authenticate(conn, dir, password);
                if (hello.GetStr("role") == "control")
                {
                    Console.WriteLine($"CONTROL hello peer={peer}");
                    var finished = new ManualResetEventSlim();
                    var link = ControlLink.FromAccepted(
                        conn,
                        peer,
                        onText: (text, _) => Console.WriteLine($"SAY {text}"),
                        onState: up =>
                        {
                            Console.WriteLine($"STATE up={up}");
                            if (!up)
                            {
                                finished.Set();
                            }
                        },
                        onPush: (name, size, token) => Console.WriteLine($"PUSH {name} size={size} token={token}"),
                        onNotice: (text, _) => Console.WriteLine($"NOTICE {text}"));
                    finished.Wait(TimeSpan.FromSeconds(3));
                    link.Close();
                    keepSocket = true;
                    handled++;
                    continue;
                }

                ServerSession.HandleTransferSession(
                    conn,
                    dir,
                    timeoutSeconds: 30,
                    locks: locks,
                    emit: PrintEvent);
                handled++;
            }
            catch (HelloRejectedException ex)
            {
                Console.WriteLine($"REJECTED {ex.Message}");
            }
            catch (ProtocolException ex)
            {
                Console.WriteLine($"PROTOCOL_ERROR {ex.Message}");
            }
            catch (Exception ex) when (ErrorKinds.IsTransportError(ex))
            {
                Console.WriteLine($"CONN_ERROR {ex.Message}");
            }
            finally
            {
                if (!keepSocket)
                {
                    conn.Dispose();
                }
            }
        }

        Console.WriteLine("DONE");
        return 0;
    }

    private static void PrintEvent(TransferEventArgs e)
    {
        switch (e.Type)
        {
            case "start":
                Console.WriteLine($"EVENT start {e.Direction} {e.Name} size={e.Size} resume={e.ResumedFrom}");
                break;
            case "done":
                var data = e.Result!;
                Console.WriteLine($"EVENT done {data.Direction} {data.Name} size={data.Size} matched={data.Matched} sha={data.LocalSha256}");
                break;
            case "error":
                Console.WriteLine($"EVENT error {e.Direction} {e.Name} {e.Error}");
                break;
        }
    }

    // ------------------------------------------------------------------ #
    // 客户端
    // ------------------------------------------------------------------ #

    private static int Send(string host, int port, string path, string password)
    {
        var peer = Handshake.ResolveAndConnect(
            host, port, string.Empty, 5.0, 30.0, "transfer", AppName,
            new Dictionary<string, object?> { ["client_id"] = "cspeer" }, password);
        Console.WriteLine($"CONNECTED {peer.Peer}");
        try
        {
            var result = FileTransfer.SendFile(
                peer.Conn,
                path,
                onStage: stage => Console.WriteLine($"STAGE {stage.Stage}"));
            Console.WriteLine($"RESULT ok={result.Ok} direction={result.Direction} name={result.Name} size={result.Size} " +
                              $"matched={result.Matched} sha={result.LocalSha256} peer_sha={result.PeerSha256} error={result.Error}");
            peer.Conn.SendMsg(new Dictionary<string, object?> { ["cmd"] = "bye" });
            peer.Conn.RecvMsg();
            return result.Ok ? 0 : 1;
        }
        finally
        {
            peer.Conn.Dispose();
        }
    }

    private static int Recv(string host, int port, string name, string dir, string password)
    {
        Directory.CreateDirectory(dir);
        var peer = Handshake.ResolveAndConnect(
            host, port, string.Empty, 5.0, 30.0, "transfer", AppName,
            new Dictionary<string, object?> { ["client_id"] = "cspeer" }, password);
        Console.WriteLine($"CONNECTED {peer.Peer}");
        try
        {
            var result = FileTransfer.RecvFile(peer.Conn, name, dir);
            Console.WriteLine($"RESULT ok={result.Ok} direction={result.Direction} name={result.Name} final={result.FinalName} " +
                              $"size={result.Size} matched={result.Matched} sha={result.LocalSha256} error={result.Error}");
            peer.Conn.SendMsg(new Dictionary<string, object?> { ["cmd"] = "bye" });
            peer.Conn.RecvMsg();
            return result.Ok ? 0 : 1;
        }
        finally
        {
            peer.Conn.Dispose();
        }
    }

    /// <summary>用 IPv4 连到双栈监听端口（模拟器 adb forward 测试用）。</summary>
    private static ConnectedPeer ConnectIpv4(string host, int port, string password,
        string role = "transfer", string app = AppName)
    {
        var socket = new Socket(AddressFamily.InterNetwork, SocketType.Stream, ProtocolType.Tcp);
        socket.Connect(new IPEndPoint(IPAddress.Parse(host), port));
        return Handshake.Negotiate(
            socket,
            PeerFormatter.Format(socket.RemoteEndPoint),
            role,
            app,
            new Dictionary<string, object?> { ["client_id"] = "cspeer4" },
            password,
            30);
    }

    private static int SendIpv4(string host, int port, string path, string password)
    {
        var peer = ConnectIpv4(host, port, password);
        Console.WriteLine($"CONNECTED {peer.Peer}");
        try
        {
            var result = FileTransfer.SendFile(peer.Conn, path, onStage: stage => Console.WriteLine($"STAGE {stage.Stage}"));
            Console.WriteLine($"RESULT ok={result.Ok} direction={result.Direction} name={result.Name} size={result.Size} " +
                              $"matched={result.Matched} sha={result.LocalSha256} peer_sha={result.PeerSha256} error={result.Error}");
            peer.Conn.SendMsg(new Dictionary<string, object?> { ["cmd"] = "bye" });
            peer.Conn.RecvMsg();
            return result.Ok ? 0 : 1;
        }
        finally
        {
            peer.Conn.Dispose();
        }
    }

    private static int RecvIpv4(string host, int port, string name, string dir, string password)
    {
        Directory.CreateDirectory(dir);
        var peer = ConnectIpv4(host, port, password);
        Console.WriteLine($"CONNECTED {peer.Peer}");
        try
        {
            var result = FileTransfer.RecvFile(peer.Conn, name, dir);
            Console.WriteLine($"RESULT ok={result.Ok} direction={result.Direction} name={result.Name} final={result.FinalName} " +
                              $"size={result.Size} matched={result.Matched} sha={result.LocalSha256} error={result.Error}");
            peer.Conn.SendMsg(new Dictionary<string, object?> { ["cmd"] = "bye" });
            peer.Conn.RecvMsg();
            return result.Ok ? 0 : 1;
        }
        finally
        {
            peer.Conn.Dispose();
        }
    }

    /// <summary>
    /// 行为接近 App 服务器的对端：同时接受文字通道和文件会话，连上就推送一次文件。
    /// 用于在模拟器上验证 App 的客户端侧（聊天 / 推送下载 / 上传）。
    /// </summary>
    private static int Dual(string dir, string host, int port, string pushFile)
    {
        Directory.CreateDirectory(dir);
        using var listener = Handshake.MakeListener(host, port);
        Console.WriteLine($"READY port={port}");
        var token = "tok-dual";
        var pushPath = string.IsNullOrEmpty(pushFile) ? null : pushFile;
        var locks = new NameLocks();

        while (true)
        {
            var client = listener.Accept();
            var peer = PeerFormatter.Format(client.RemoteEndPoint);
            var conn = new FramedConnection(client);
            _ = Task.Run(() =>
            {
                var keep = false;
                try
                {
                    var hello = Handshake.ReadHello(conn);
                    Handshake.Authenticate(conn, dir, string.Empty);
                    var role = hello.GetStr("role");
                    Console.WriteLine($"HELLO role={role} peer={peer} client_id={hello.GetStr("client_id")}");
                    if (role == "control")
                    {
                        ControlLink? link = null;
                        link = ControlLink.FromAccepted(
                            conn,
                            peer,
                            onText: (text, _) =>
                            {
                                Console.WriteLine($"SAY {text}");
                                try
                                {
                                    link?.SendNotice("收到：" + text);
                                }
                                catch (Exception)
                                {
                                    // 忽略
                                }
                            },
                            onNotice: (text, _) => Console.WriteLine($"NOTICE {text}"),
                            onPush: (name, size, _) => Console.WriteLine($"PUSH {name} size={size}"),
                            onState: up => Console.WriteLine($"STATE up={up}"));
                        keep = true;
                        if (pushPath is not null && File.Exists(pushPath))
                        {
                            Thread.Sleep(400);
                            try
                            {
                                link.SendPush(Path.GetFileName(pushPath), new FileInfo(pushPath).Length, token);
                                Console.WriteLine("PUSH_SENT");
                            }
                            catch (Exception ex)
                            {
                                Console.WriteLine($"PUSH_FAIL {ex.Message}");
                            }
                        }

                        return;
                    }

                    ServerSession.HandleTransferSession(
                        conn,
                        dir,
                        120,
                        locks,
                        PrintEvent,
                        requested => requested == token ? pushPath : null);
                }
                catch (Exception ex)
                {
                    Console.WriteLine($"SESSION_END {ex.GetType().Name}: {ex.Message}");
                }
                finally
                {
                    if (!keep)
                    {
                        conn.Dispose();
                    }
                }
            });
        }
    }

    /// <summary>保持一条文字通道（模拟器测试用），期间打印对端发来的所有消息。</summary>
    private static int Hold4(string host, int port, int seconds, string password)
    {
        var connected = ConnectIpv4(host, port, password, "control", "file_gui/1.0");
        var closed = new ManualResetEventSlim();
        var link = ControlLink.FromAccepted(
            connected.Conn,
            connected.Peer,
            onText: (value, _) => Console.WriteLine($"SAY {value}"),
            onNotice: (value, _) => Console.WriteLine($"NOTICE {value}"),
            onPush: (name, size, token) => Console.WriteLine($"PUSH {name} size={size} token={token}"),
            onState: up =>
            {
                Console.WriteLine($"STATE up={up}");
                if (!up)
                {
                    closed.Set();
                }
            });
        Console.WriteLine($"HOLDING {seconds}s peer={connected.Peer}");
        closed.Wait(TimeSpan.FromSeconds(seconds));
        link.Close();
        Console.WriteLine("HOLD_END");
        return 0;
    }

    /// <summary>用 IPv4 连到双栈监听端口并跑文字通道（模拟器测试用）。</summary>
    private static int ControlSendIpv4(string host, int port, string text, string password)
    {
        var connected = ConnectIpv4(host, port, password, "control", "file_gui/1.0");
        Console.WriteLine($"CONNECTED {connected.Peer}");
        var notices = 0;
        var finished = new ManualResetEventSlim();
        var link = ControlLink.FromAccepted(
            connected.Conn,
            connected.Peer,
            onText: (value, _) => Console.WriteLine($"SAY {value}"),
            onState: up =>
            {
                Console.WriteLine($"STATE up={up}");
                if (!up)
                {
                    finished.Set();
                }
            },
            onPush: (name, size, token) => Console.WriteLine($"PUSH {name} size={size} token={token}"),
            onNotice: (value, _) =>
            {
                Console.WriteLine($"NOTICE {value}");
                Interlocked.Increment(ref notices);
            });
        try
        {
            link.SendText(text);
            finished.Wait(TimeSpan.FromSeconds(4));
            Console.WriteLine($"RESULT notices={notices}");
            return 0;
        }
        finally
        {
            link.Close();
        }
    }

    // ------------------------------------------------------------------ #
    // 文字通道
    // ------------------------------------------------------------------ #

    private static int ControlServe(string host, int port, string password)
    {
        using var listener = Handshake.MakeListener(host, port);
        Console.WriteLine($"READY port={port}");
        var client = listener.Accept();
        var peer = PeerFormatter.Format(client.RemoteEndPoint);
        var conn = new FramedConnection(client);
        try
        {
            var hello = Handshake.ReadHello(conn);
            Handshake.Authenticate(conn, Path.GetTempPath(), password);
            Console.WriteLine($"HELLO role={hello.GetStr("role")} client_id={hello.GetStr("client_id")}");

            var finished = new ManualResetEventSlim();
            ControlLink? link = null;
            link = ControlLink.FromAccepted(
                conn,
                peer,
                onText: (text, _) =>
                {
                    Console.WriteLine($"SAY {text}");
                    try
                    {
                        link?.SendNotice("收到：" + text);
                        link?.SendPush("pushed.bin", 1234, "tok-1");
                    }
                    catch (Exception ex)
                    {
                        Console.WriteLine($"SEND_FAIL {ex.Message}");
                    }

                    finished.Set();
                },
                onState: up =>
                {
                    Console.WriteLine($"STATE up={up}");
                    if (!up)
                    {
                        finished.Set();
                    }
                });
            finished.Wait(TimeSpan.FromSeconds(10));
            link.Close();
        }
        finally
        {
            conn.Dispose();
        }

        Console.WriteLine("DONE");
        return 0;
    }

    private static int ControlSend(string host, int port, string text, string password)
    {
        var notices = 0;
        var pushes = 0;
        var done = new ManualResetEventSlim();
        var link = ControlLink.Connect(
            host,
            port,
            timeoutSeconds: 30,
            connectTimeoutSeconds: 5,
            password: password,
            onText: (value, _) => Console.WriteLine($"SAY {value}"),
            onNotice: (value, _) =>
            {
                Console.WriteLine($"NOTICE {value}");
                Interlocked.Increment(ref notices);
                if (Volatile.Read(ref notices) >= 1 && Volatile.Read(ref pushes) >= 1)
                {
                    done.Set();
                }
            },
            onPush: (name, size, token) =>
            {
                Console.WriteLine($"PUSH {name} size={size} token={token}");
                Interlocked.Increment(ref pushes);
                if (Volatile.Read(ref notices) >= 1 && Volatile.Read(ref pushes) >= 1)
                {
                    done.Set();
                }
            },
            onState: up =>
            {
                if (!up)
                {
                    done.Set();
                }
            },
            clientId: "csclient");
        Console.WriteLine($"CONNECTED {link.Peer}");
        try
        {
            link.SendText(text);
            done.Wait(TimeSpan.FromSeconds(8));
            Console.WriteLine($"RESULT notices={notices} pushes={pushes}");
            return notices >= 1 && pushes >= 1 ? 0 : 1;
        }
        finally
        {
            link.Close();
        }
    }

    // ------------------------------------------------------------------ #
    // 服务器推送文件 → 客户端带 token 下载
    // ------------------------------------------------------------------ #

    private static int PushServe(string dir, string host, int port, string file, string password)
    {
        Directory.CreateDirectory(dir);
        const string token = "tok-cs";
        using var listener = Handshake.MakeListener(host, port);
        Console.WriteLine($"READY port={port} name={Path.GetFileName(file)} size={new FileInfo(file).Length}");

        var control = listener.Accept();
        var controlPeer = PeerFormatter.Format(control.RemoteEndPoint);
        var controlConn = new FramedConnection(control);
        var hello = Handshake.ReadHello(controlConn);
        Handshake.Authenticate(controlConn, dir, password);
        Console.WriteLine($"CONTROL peer={controlPeer} role={hello.GetStr("role")}");
        var link = ControlLink.FromAccepted(controlConn, controlPeer);
        link.SendPush(Path.GetFileName(file), new FileInfo(file).Length, token);
        Console.WriteLine("PUSH_SENT");

        var transfer = listener.Accept();
        var transferPeer = PeerFormatter.Format(transfer.RemoteEndPoint);
        var transferConn = new FramedConnection(transfer);
        try
        {
            Handshake.ReadHello(transferConn);
            Handshake.Authenticate(transferConn, dir, password);
            ServerSession.HandleTransferSession(
                transferConn,
                dir,
                timeoutSeconds: 30,
                emit: PrintEvent,
                pathResolver: requested => requested == token && File.Exists(file) ? file : null);
            Console.WriteLine($"SERVED {transferPeer}");
        }
        finally
        {
            transferConn.Dispose();
        }

        link.SendNotice("文件已发送");
        Thread.Sleep(300);
        link.Close();
        Console.WriteLine("DONE");
        return 0;
    }

    private static int PushRecv(string host, int port, string dir, string password)
    {
        Directory.CreateDirectory(dir);
        var done = new ManualResetEventSlim();
        TransferResult? result = null;
        string? failure = null;
        ControlLink? link = null;

        void OnPush(string name, long size, string? token)
        {
            Console.WriteLine($"PUSH {name} size={size} token={token}");
            try
            {
                var peer = Handshake.ResolveAndConnect(
                    host, port, string.Empty, 5.0, 30.0, "transfer", AppName,
                    new Dictionary<string, object?> { ["client_id"] = "cspeer" }, password);
                try
                {
                    result = FileTransfer.RecvFile(peer.Conn, name, dir, token: token);
                    Console.WriteLine($"RESULT ok={result.Ok} name={result.FinalName} size={result.Size} matched={result.Matched} error={result.Error}");
                    Console.WriteLine("BYE_SENT");
                }
                finally
                {
                    peer.Conn.Dispose();
                }
            }
            catch (Exception ex)
            {
                failure = ex.Message;
                Console.WriteLine($"FAIL {ex.Message}");
            }
            finally
            {
                done.Set();
            }
        }

        link = ControlLink.Connect(
            host,
            port,
            timeoutSeconds: 30,
            connectTimeoutSeconds: 5,
            password: password,
            onPush: OnPush,
            onNotice: (text, _) => Console.WriteLine($"NOTICE {text}"),
            onState: up =>
            {
                if (!up)
                {
                    done.Set();
                }
            },
            clientId: "cspeer");
        Console.WriteLine($"CONNECTED {link.Peer}");
        try
        {
            done.Wait(TimeSpan.FromSeconds(20));
            return result is { Ok: true } && failure is null ? 0 : 1;
        }
        finally
        {
            link.Close();
        }
    }

    // ------------------------------------------------------------------ #
    // 单机自测：上传 / 回传 / 续传 / 聊天
    // ------------------------------------------------------------------ #

    private static int SelfTest(string dir)
    {
        Directory.CreateDirectory(dir);
        var failures = new List<string>();
        var sourceDir = Path.Combine(dir, "src");
        var serverDir = Path.Combine(dir, "server");
        var backDir = Path.Combine(dir, "back");
        Directory.CreateDirectory(sourceDir);
        Directory.CreateDirectory(serverDir);
        Directory.CreateDirectory(backDir);

        var payload = new byte[3 * 1024 * 1024 + 12345];
        new Random(42).NextBytes(payload);
        var uploadPath = Path.Combine(sourceDir, "测试文件.bin");
        File.WriteAllBytes(uploadPath, payload);
        var expectedHash = Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(payload)).ToLowerInvariant();

        using var listener = Handshake.MakeListener("::1", 0);
        var port = ((IPEndPoint)listener.LocalEndPoint!).Port;
        Console.WriteLine($"selftest port={port}");

        void ServerLoop(int sessions, string root)
        {
            for (var i = 0; i < sessions; i++)
            {
                var client = listener.Accept();
                var conn = new FramedConnection(client);
                try
                {
                    var hello = Handshake.ReadHello(conn);
                    Handshake.Authenticate(conn, root, string.Empty);
                    if (hello.GetStr("role") == "control")
                    {
                        var link = ControlLink.FromAccepted(
                            conn,
                            PeerFormatter.Format(client.RemoteEndPoint),
                            onText: (text, _) =>
                            {
                                Console.WriteLine($"SERVER SAY {text}");
                            });
                        Thread.Sleep(1500);
                        link.Close();
                        continue;
                    }

                    ServerSession.HandleTransferSession(conn, root, 30, emit: PrintEvent);
                }
                finally
                {
                    conn.Dispose();
                }
            }
        }

        var serverThread = new Thread(() => ServerLoop(4, serverDir)) { IsBackground = true };
        serverThread.Start();

        // 1) 上传
        var upload = Handshake.ResolveAndConnect("::1", port, string.Empty, 5, 30, "transfer", AppName,
            new Dictionary<string, object?> { ["client_id"] = "self" }, string.Empty);
        var sendResult = FileTransfer.SendFile(upload.Conn, uploadPath);
        upload.Conn.SendMsg(new Dictionary<string, object?> { ["cmd"] = "bye" });
        upload.Conn.RecvMsg();
        upload.Conn.Dispose();
        Console.WriteLine($"UPLOAD ok={sendResult.Ok} matched={sendResult.Matched} sha={sendResult.LocalSha256}");
        if (!sendResult.Ok || !sendResult.Matched)
        {
            failures.Add("上传失败");
        }

        var uploaded = Path.Combine(serverDir, sendResult.FinalName);
        if (!File.Exists(uploaded) || ServerSession.Sha256OfFile(uploaded) != expectedHash)
        {
            failures.Add("服务器端文件哈希不匹配");
        }

        // 2) 回传（下载）
        var download = Handshake.ResolveAndConnect("::1", port, string.Empty, 5, 30, "transfer", AppName,
            new Dictionary<string, object?> { ["client_id"] = "self" }, string.Empty);
        var recvResult = FileTransfer.RecvFile(download.Conn, sendResult.FinalName, backDir);
        download.Conn.SendMsg(new Dictionary<string, object?> { ["cmd"] = "bye" });
        download.Conn.RecvMsg();
        download.Conn.Dispose();
        Console.WriteLine($"DOWNLOAD ok={recvResult.Ok} matched={recvResult.Matched} file={recvResult.FinalName}");
        if (!recvResult.Ok || ServerSession.Sha256OfFile(recvResult.Path) != expectedHash)
        {
            failures.Add("下载内容不一致");
        }

        // 3) 续传：手工放一个半截 .part，服务器应返回 resume_from
        var resumeName = sendResult.FinalName;
        var partTarget = Path.Combine(backDir, resumeName + ".part");
        var partBytes = payload.AsSpan(0, 500_000).ToArray();
        File.WriteAllBytes(partTarget, partBytes);
        var resume = Handshake.ResolveAndConnect("::1", port, string.Empty, 5, 30, "transfer", AppName,
            new Dictionary<string, object?> { ["client_id"] = "self" }, string.Empty);
        var resumeResult = FileTransfer.RecvFile(resume.Conn, resumeName, backDir);
        resume.Conn.SendMsg(new Dictionary<string, object?> { ["cmd"] = "bye" });
        resume.Conn.RecvMsg();
        resume.Conn.Dispose();
        Console.WriteLine($"RESUME ok={resumeResult.Ok} final={resumeResult.FinalName} from={resumeResult.ResumedFrom}");
        if (!resumeResult.Ok || resumeResult.ResumedFrom != partBytes.Length)
        {
            failures.Add($"续传失败（resumed_from={resumeResult.ResumedFrom}）");
        }
        else if (ServerSession.Sha256OfFile(resumeResult.Path) != expectedHash)
        {
            failures.Add("续传内容哈希不一致");
        }

        // 4) 聊天通道
        var chatDone = new ManualResetEventSlim();
        var link = ControlLink.Connect(
            "::1",
            port,
            timeoutSeconds: 10,
            connectTimeoutSeconds: 5,
            onText: (text, _) =>
            {
                Console.WriteLine($"CLIENT SAY {text}");
                chatDone.Set();
            },
            onState: up =>
            {
                if (!up)
                {
                    chatDone.Set();
                }
            },
            clientId: "self");
        link.SendText("你好，自测");
        link.Close();

        Thread.Sleep(300);
        serverThread.Join(TimeSpan.FromSeconds(10));
        Console.WriteLine(failures.Count == 0 ? "SELFTEST PASS" : $"SELFTEST FAIL: {string.Join("；", failures)}");
        return failures.Count == 0 ? 0 : 1;
    }
}
