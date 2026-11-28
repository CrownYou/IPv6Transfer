using System;
using System.Collections.Generic;
using System.Globalization;
using System.Net;
using System.Net.NetworkInformation;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;

namespace Ipv6Transfer.Core.Protocol;

/// <summary>成功连上对端后返回的东西。</summary>
public sealed class ConnectedPeer
{
    public ConnectedPeer(FramedConnection conn, string peer, Dictionary<string, object?> reply)
    {
        Conn = conn;
        Peer = peer;
        Reply = reply;
    }

    public FramedConnection Conn { get; }
    public string Peer { get; }
    public Dictionary<string, object?> Reply { get; }
}

/// <summary>
/// 握手与密码认证：hello → （可选）HMAC-SHA256 挑战应答 → ack。
/// 密码不在网络上明文传输，与 python 版完全一致。
/// </summary>
public static class Handshake
{
    /// <summary>服务端读取并校验 hello；版本不符时先回错误再抛异常。</summary>
    public static Dictionary<string, object?> ReadHello(FramedConnection conn)
    {
        var msg = conn.RecvMsg();
        if (msg.GetStr("cmd") != "hello")
        {
            throw new ProtocolException("期望 hello 握手");
        }

        long version;
        if (!msg.TryGetValue("version", out var raw) || raw is null)
        {
            version = 0;
        }
        else
        {
            version = raw switch
            {
                bool => throw new ProtocolException("字段 version 必须是整数"),
                long l => l,
                int i => i,
                _ => throw new ProtocolException("字段 version 必须是整数"),
            };
        }

        if (version != ProtocolConst.ProtoVersion)
        {
            conn.SendMsg(new Dictionary<string, object?>
            {
                ["ok"] = false,
                ["error"] = $"协议版本不匹配（服务器 {ProtocolConst.ProtoVersion}，客户端 {version}）",
            });
            throw new HelloRejectedException($"协议版本不匹配（客户端 {version}）");
        }

        return msg;
    }

    /// <summary>HMAC-SHA256(password, nonce) 的小写十六进制摘要。</summary>
    public static string AuthDigest(string password, string nonce)
    {
        using var hmac = new HMACSHA256(Encoding.UTF8.GetBytes(password ?? string.Empty));
        var digest = hmac.ComputeHash(Encoding.ASCII.GetBytes(nonce ?? string.Empty));
        return Convert.ToHexString(digest).ToLowerInvariant();
    }

    public static void AckHello(FramedConnection conn, string rootDir)
    {
        conn.SendMsg(new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["version"] = ProtocolConst.ProtoVersion,
            ["root"] = Path.GetFullPath(rootDir),
        });
    }

    /// <summary>在 ReadHello 之后执行密码认证并回送 ack。</summary>
    public static void Authenticate(FramedConnection conn, string rootDir, string password)
    {
        password ??= string.Empty;
        if (password.Length > ProtocolConst.MaxPasswordChars)
        {
            throw new ProtocolException($"连接密码过长（最多 {ProtocolConst.MaxPasswordChars} 个字符）");
        }

        if (password.Length > 0)
        {
            var nonceBytes = RandomNumberGenerator.GetBytes(16);
            var nonce = Convert.ToHexString(nonceBytes).ToLowerInvariant();
            conn.SendMsg(new Dictionary<string, object?>
            {
                ["ok"] = false,
                ["auth_required"] = true,
                ["method"] = ProtocolConst.AuthMethod,
                ["nonce"] = nonce,
                ["error"] = "服务器已启用连接密码",
            });

            var reply = conn.RecvMsg();
            var response = reply.GetStr("response");
            if (reply.GetStr("cmd") != "auth" || response is null)
            {
                conn.SendMsg(new Dictionary<string, object?> { ["ok"] = false, ["error"] = "客户端未提供连接密码" });
                throw new HelloRejectedException("客户端未完成密码认证");
            }

            var expected = AuthDigest(password, nonce);
            if (!CryptographicOperations.FixedTimeEquals(
                    Encoding.ASCII.GetBytes(expected), Encoding.ASCII.GetBytes(response.ToLowerInvariant())))
            {
                conn.SendMsg(new Dictionary<string, object?> { ["ok"] = false, ["error"] = "连接密码错误" });
                throw new HelloRejectedException("连接密码错误");
            }
        }

        AckHello(conn, rootDir);
    }

    /// <summary>读取 hello、完成密码认证并 ack；返回 hello 内容。</summary>
    public static Dictionary<string, object?> AcceptHello(FramedConnection conn, string rootDir, string password)
    {
        var hello = ReadHello(conn);
        Authenticate(conn, rootDir, password);
        return hello;
    }

    // ------------------------------------------------------------------ #
    // 建立连接（客户端）
    // ------------------------------------------------------------------ #

    /// <summary>解析 IPv6 地址并尝试连接，成功后完成 hello/密码握手。</summary>
    public static ConnectedPeer ResolveAndConnect(
        string host,
        int port,
        string zone = "",
        double connectTimeoutSeconds = 10.0,
        double timeoutSeconds = 120.0,
        string role = "transfer",
        string app = "",
        Dictionary<string, object?>? extraHello = null,
        string password = "")
    {
        host = (host ?? string.Empty).Trim();
        if (host.StartsWith("[", StringComparison.Ordinal) && host.EndsWith("]", StringComparison.Ordinal))
        {
            host = host.Substring(1, host.Length - 2);
        }

        if (zone.Length > 0 && !host.Contains('%'))
        {
            host = $"{host}%{zone}";
        }

        var address = TryParseIPv6(host);
        if (address is null)
        {
            throw new ConnectException(
                $"无法解析 IPv6 地址 [{host}]:{port}\n" +
                "  提示：链路本地地址需用「接口索引」指定出口网卡；确认该地址确实属于对端的 IPv6 接口。");
        }

        var endpoint = new IPEndPoint(address, port);
        var socket = new Socket(AddressFamily.InterNetworkV6, SocketType.Stream, ProtocolType.Tcp);
        try
        {
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(Math.Max(connectTimeoutSeconds, 0.5)));
            socket.ConnectAsync(endpoint, cts.Token).AsTask().GetAwaiter().GetResult();
        }
        catch (OperationCanceledException)
        {
            socket.Dispose();
            throw new ConnectException($"连接 [{host}]:{port} 超时（{connectTimeoutSeconds:0.#} 秒）\n" +
                                       "  排查：1) 对端是否已启动监听且端口正确；2) 两端 ping6 是否互通；" +
                                       "3) 对端防火墙是否放行该端口。");
        }
        catch (SocketException ex)
        {
            var detail = $"{address}: {ex.SocketErrorCode}";
            socket.Dispose();
            throw new ConnectException($"连接 [{host}]:{port} 失败（{detail}）\n" +
                                       "  排查：1) 对端是否已启动监听且端口正确；2) 两端 ping6 是否互通；" +
                                       "3) 对端防火墙是否放行该端口。");
        }

        return Negotiate(socket, PeerFormatter.Format(socket.RemoteEndPoint), role, app, extraHello, password, timeoutSeconds);
    }

    /// <summary>
    /// 在一个已连接的 Socket 上完成 hello / 密码认证；返回可用连接。
    /// 单独暴露出来是为了支持测试工具用 IPv4 连到双栈监听端口。
    /// </summary>
    public static ConnectedPeer Negotiate(
        Socket socket,
        string peer,
        string role = "transfer",
        string app = "",
        Dictionary<string, object?>? extraHello = null,
        string password = "",
        double timeoutSeconds = 120.0)
    {
        socket.ReceiveTimeout = (int)Math.Round(timeoutSeconds * 1000);
        socket.SendTimeout = (int)Math.Round(timeoutSeconds * 1000);
        var conn = new FramedConnection(socket);
        try
        {
            var hello = new Dictionary<string, object?>
            {
                ["cmd"] = "hello",
                ["version"] = ProtocolConst.ProtoVersion,
                ["role"] = role,
                ["app"] = app,
            };
            if (extraHello is not null)
            {
                foreach (var pair in extraHello)
                {
                    hello[pair.Key] = pair.Value;
                }
            }

            conn.SendMsg(hello);
            var reply = conn.RecvMsg();
            if (reply.GetBool("auth_required"))
            {
                var nonce = reply.GetStr("nonce");
                if (string.IsNullOrEmpty(nonce))
                {
                    conn.Dispose();
                    throw new ConnectException("服务器要求密码认证，但握手数据不完整");
                }

                if (string.IsNullOrEmpty(password))
                {
                    try
                    {
                        conn.SendMsg(new Dictionary<string, object?> { ["cmd"] = "auth", ["response"] = "" });
                        conn.RecvMsg();
                    }
                    catch (Exception ex) when (ErrorKinds.IsTransportError(ex))
                    {
                        // 对端可能已经关闭，忽略
                    }

                    conn.Dispose();
                    throw new ConnectException("服务器已启用连接密码，请在客户端填写密码后重试");
                }

                conn.SendMsg(new Dictionary<string, object?>
                {
                    ["cmd"] = "auth",
                    ["response"] = AuthDigest(password, nonce),
                });
                reply = conn.RecvMsg();
            }

            if (!reply.GetBool("ok"))
            {
                conn.Dispose();
                throw new ConnectException($"对端拒绝连接：{reply.GetStr("error")}");
            }

            return new ConnectedPeer(conn, peer, reply);
        }
        catch (Exception ex) when (ex is not ConnectException)
        {
            conn.Dispose();
            throw new ConnectException($"握手失败：{ex.Message}", ex);
        }
    }

    /// <summary>把 "fe80::1%12"、"[2409::1]" 之类的文本解析成 IPv6 地址。</summary>
    public static IPAddress? TryParseIPv6(string host)
    {
        if (string.IsNullOrWhiteSpace(host))
        {
            return null;
        }

        var text = host.Trim();
        if (text.StartsWith("[", StringComparison.Ordinal) && text.EndsWith("]", StringComparison.Ordinal))
        {
            text = text.Substring(1, text.Length - 2);
        }

        string? scopeText = null;
        var percent = text.IndexOf('%');
        if (percent >= 0)
        {
            scopeText = text.Substring(percent + 1);
            text = text.Substring(0, percent);
        }

        if (!IPAddress.TryParse(text, out var address)
            || address.AddressFamily != AddressFamily.InterNetworkV6)
        {
            // 允许直接填接口名（Android 上 wlan0 这类），由系统换成接口索引
            if (scopeText is not null && IPAddress.TryParse(text, out var bare)
                && bare.AddressFamily == AddressFamily.InterNetworkV6)
            {
                address = bare;
            }
            else
            {
                return null;
            }
        }

        if (scopeText is not null)
        {
            var scopeId = ResolveScopeId(scopeText);
            if (scopeId is null)
            {
                return null;
            }

            address.ScopeId = scopeId.Value;
        }

        return address;
    }

    private static long? ResolveScopeId(string scopeText)
    {
        if (long.TryParse(scopeText, NumberStyles.Integer, CultureInfo.InvariantCulture, out var index))
        {
            return index;
        }

        try
        {
            foreach (var nic in NetworkInterface.GetAllNetworkInterfaces())
            {
                if (string.Equals(nic.Name, scopeText, StringComparison.OrdinalIgnoreCase))
                {
                    return nic.GetIPProperties().GetIPv6Properties()?.Index ?? 0;
                }
            }
        }
        catch (Exception)
        {
            // 取不到接口序号就当作失败
        }

        return null;
    }

    /// <summary>服务器监听：绑定 [::]:port，默认双栈（与 python 的 IPV6_V6ONLY=0 一致）。</summary>
    public static Socket MakeListener(string host, int port, bool ipv6Only = false)
    {
        var socket = new Socket(AddressFamily.InterNetworkV6, SocketType.Stream, ProtocolType.Tcp);
        try
        {
            socket.SetSocketOption(SocketOptionLevel.Socket, SocketOptionName.ReuseAddress, true);
        }
        catch (Exception)
        {
            // 部分平台不支持，忽略
        }

        try
        {
            socket.DualMode = !ipv6Only;
        }
        catch (Exception)
        {
            // iOS 等平台可能不支持双栈，退化成纯 IPv6
        }

        var address = string.IsNullOrWhiteSpace(host) || host == "::"
            ? IPAddress.IPv6Any
            : TryParseIPv6(host) ?? IPAddress.IPv6Any;
        socket.Bind(new IPEndPoint(address, port));
        socket.Listen(64);
        return socket;
    }
}
