using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;

namespace Ipv6Transfer.Core.Protocol;

/// <summary>
/// 带缓冲的帧收发器：JSON 控制行 + 按声明长度直接跟随的裸字节。
/// 对应 python 侧的 <c>class Conn</c>。
/// </summary>
public sealed class FramedConnection : IDisposable
{
    private readonly Socket _socket;
    private readonly List<byte> _pending = new(4096);
    private bool _disposed;

    public FramedConnection(Socket socket)
    {
        _socket = socket ?? throw new ArgumentNullException(nameof(socket));
    }

    public Socket Socket => _socket;

    public bool IsDisposed => _disposed;

    /// <summary>Peer 的可读形式，例如 <c>[2409::1]:9000</c>。</summary>
    public string Peer => PeerFormatter.Format(_socket.RemoteEndPoint);

    /// <summary>设置阻塞读/写超时；null 表示不限（文字通道允许长时间空闲）。</summary>
    public void SetTimeout(int? milliseconds)
    {
        var value = milliseconds ?? 0;
        try
        {
            _socket.ReceiveTimeout = value;
            _socket.SendTimeout = value;
        }
        catch (ObjectDisposedException)
        {
            // 已经关掉的连接，设置超时没有意义
        }
    }

    // -- 控制消息 -----------------------------------------------------------

    /// <summary>读一行（不含结尾的 \n）。</summary>
    public byte[] ReadLine()
    {
        while (true)
        {
            var index = _pending.IndexOf((byte)'\n');
            if (index >= 0)
            {
                var line = new byte[index];
                _pending.CopyTo(0, line, 0, index);
                _pending.RemoveRange(0, index + 1);
                return line;
            }

            if (_pending.Count > ProtocolConst.MaxLine)
            {
                throw new ProtocolException("控制消息过长");
            }

            var chunk = new byte[65536];
            int got;
            try
            {
                got = _socket.Receive(chunk, 0, chunk.Length, SocketFlags.None);
            }
            catch (SocketException ex) when (ex.SocketErrorCode == SocketError.TimedOut)
            {
                // 与 python 的 socket.timeout 一致：超时属于可捕获的传输错误
                throw new IOException("等待对端响应超时", ex);
            }

            if (got <= 0)
            {
                throw new IOException("对端已关闭连接");
            }

            for (var i = 0; i < got; i++)
            {
                _pending.Add(chunk[i]);
            }
        }
    }

    public Dictionary<string, object?> RecvMsg()
    {
        var line = ReadLine();
        var text = Encoding.UTF8.GetString(line).Trim();
        if (text.Length == 0)
        {
            throw new ProtocolException("收到空控制消息");
        }

        return JsonMsg.ParseObject(Encoding.UTF8.GetBytes(text));
    }

    public void SendMsg(IDictionary<string, object?> message)
    {
        var payload = JsonMsg.Serialize(message);
        if (payload.Length + 1 > ProtocolConst.MaxLine)
        {
            throw new ProtocolException("要发送的控制消息过长");
        }

        var frame = new byte[payload.Length + 1];
        Buffer.BlockCopy(payload, 0, frame, 0, payload.Length);
        frame[^1] = (byte)'\n';
        SendAll(frame, 0, frame.Length);
    }

    // -- 文件内容 -----------------------------------------------------------

    /// <summary>读取 count 字节写入 target；每读到一块就回调 onChunk。</summary>
    public void RecvToStream(Stream target, long count, Action<ReadOnlySpan<byte>>? onChunk = null)
    {
        var remaining = count;
        if (remaining <= 0)
        {
            return;
        }

        if (_pending.Count > 0)
        {
            var take = (int)Math.Min(remaining, _pending.Count);
            var head = new byte[take];
            _pending.CopyTo(0, head, 0, take);
            _pending.RemoveRange(0, take);
            target.Write(head, 0, take);
            onChunk?.Invoke(head);
            remaining -= take;
        }

        if (remaining <= 0)
        {
            return;
        }

        var buffer = new byte[(int)Math.Min(ProtocolConst.ChunkSize, remaining)];
        while (remaining > 0)
        {
            var want = (int)Math.Min(buffer.Length, remaining);
            int got;
            try
            {
                got = _socket.Receive(buffer, 0, want, SocketFlags.None);
            }
            catch (SocketException ex) when (ex.SocketErrorCode == SocketError.TimedOut)
            {
                throw new IOException("接收文件数据超时", ex);
            }

            if (got <= 0)
            {
                throw new IOException("对端在文件数据中途关闭了连接");
            }

            target.Write(buffer, 0, got);
            onChunk?.Invoke(buffer.AsSpan(0, got));
            remaining -= got;
        }
    }

    /// <summary>把 source 中 count 字节写入连接；每发一块就回调 onChunk。</summary>
    public void SendFromStream(Stream source, long count, Action<ReadOnlySpan<byte>>? onChunk = null)
    {
        var remaining = count;
        if (remaining <= 0)
        {
            return;
        }

        var buffer = new byte[(int)Math.Min(ProtocolConst.ChunkSize, remaining)];
        while (remaining > 0)
        {
            var want = (int)Math.Min(buffer.Length, remaining);
            var got = source.Read(buffer, 0, want);
            if (got <= 0)
            {
                throw new ProtocolException("本地文件比声明的长度短");
            }

            SendAll(buffer, 0, got);
            onChunk?.Invoke(buffer.AsSpan(0, got));
            remaining -= got;
        }
    }

    private void SendAll(byte[] buffer, int offset, int count)
    {
        var sent = 0;
        while (sent < count)
        {
            int n;
            try
            {
                n = _socket.Send(buffer, offset + sent, count - sent, SocketFlags.None);
            }
            catch (SocketException ex) when (ex.SocketErrorCode == SocketError.TimedOut)
            {
                throw new IOException("发送数据超时", ex);
            }

            if (n <= 0)
            {
                throw new IOException("发送数据失败：对端已关闭连接");
            }

            sent += n;
        }
    }

    /// <summary>主动关闭读端，让阻塞中的接收立刻返回（用于取消传输）。</summary>
    public void Shutdown()
    {
        try
        {
            _socket.Shutdown(SocketShutdown.Both);
        }
        catch (Exception)
        {
            // 已经断开
        }
    }

    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }

        _disposed = true;
        try
        {
            _socket.Dispose();
        }
        catch (Exception)
        {
            // 忽略关闭异常
        }
    }
}

/// <summary>对端地址的可读格式化，与 python 的 format_peer 一致。</summary>
public static class PeerFormatter
{
    public static string Format(EndPoint? endpoint)
    {
        switch (endpoint)
        {
            case IPEndPoint ip:
            {
                var host = ip.Address.ToString();
                if (host.StartsWith("::ffff:", StringComparison.OrdinalIgnoreCase))
                {
                    return $"{host.Substring(7)}:{ip.Port} (IPv4-mapped)";
                }

                return $"[{host}]:{ip.Port}";
            }
            case null:
                return "未知对端";
            default:
                return endpoint.ToString() ?? "未知对端";
        }
    }

    /// <summary>从 "别名 ([addr]:port)" 之类的文本里取出 IP 部分。</summary>
    public static string IpOf(string? peer)
    {
        var text = (peer ?? string.Empty).Trim();
        if (text.Length == 0)
        {
            return string.Empty;
        }

        if (text.StartsWith("[", StringComparison.Ordinal) && text.Contains("]:"))
        {
            return text.Substring(1, text.IndexOf("]:", StringComparison.Ordinal) - 1);
        }

        if (text.StartsWith("[", StringComparison.Ordinal) && text.EndsWith("]", StringComparison.Ordinal))
        {
            return text.Substring(1, text.Length - 2);
        }

        var colon = text.LastIndexOf(':');
        return colon >= 0 ? text.Substring(0, colon) : text;
    }
}
