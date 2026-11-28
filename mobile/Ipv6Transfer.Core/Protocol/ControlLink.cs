using System;
using System.Collections.Generic;
using System.IO;
using System.Threading;

namespace Ipv6Transfer.Core.Protocol;

/// <summary>
/// 文字通道（独立于文件连接）：聊天 say、系统通知 notice、文件推送 push、心跳 ping。
/// 对应 python 的 <c>class ControlLink</c>。
/// </summary>
public sealed class ControlLink : IDisposable
{
    private readonly FramedConnection _conn;
    private readonly object _writeLock = new();
    private readonly Thread _reader;
    private readonly Thread _keepalive;
    private volatile bool _closed;
    private bool _closing;
    private bool _disposed;

    private readonly Action<string, long>? _onText;
    private readonly Action<bool>? _onState;
    private readonly Action<string, long, string?>? _onPush;
    private readonly Action<string, long>? _onNotice;

    private ControlLink(
        FramedConnection conn,
        string peer,
        Action<string, long>? onText,
        Action<bool>? onState,
        Action<string, long, string?>? onPush,
        Action<string, long>? onNotice)
    {
        _conn = conn;
        Peer = peer;
        _onText = onText;
        _onState = onState;
        _onPush = onPush;
        _onNotice = onNotice;
        _reader = new Thread(ReadLoop) { IsBackground = true, Name = "control-reader" };
        _keepalive = new Thread(PingLoop) { IsBackground = true, Name = "control-ping" };
    }

    /// <summary>对端地址文本，例如 <c>[2409::1]:9000</c>。</summary>
    public string Peer { get; }

    public bool IsClosed => _closed;

    /// <summary>客户端视角：连上对端并启动文字通道。</summary>
    public static ControlLink Connect(
        string host,
        int port,
        string zone = "",
        double timeoutSeconds = 120.0,
        double connectTimeoutSeconds = 10.0,
        Action<string, long>? onText = null,
        Action<bool>? onState = null,
        Action<string, long, string?>? onPush = null,
        Action<string, long>? onNotice = null,
        string clientId = "",
        string password = "")
    {
        var extraHello = new Dictionary<string, object?>();
        if (!string.IsNullOrEmpty(clientId))
        {
            extraHello["client_id"] = clientId;
        }

        var connected = Handshake.ResolveAndConnect(
            host,
            port,
            zone,
            connectTimeoutSeconds,
            timeoutSeconds,
            role: "control",
            app: "file_gui/1.0",
            extraHello: extraHello.Count > 0 ? extraHello : null,
            password: password);

        var link = new ControlLink(connected.Conn, connected.Peer, onText, onState, onPush, onNotice);
        link._conn.SetTimeout(null); // 文字通道允许长时间空闲
        link.Start();
        return link;
    }

    /// <summary>服务端视角：把一个已经完成握手的连接升级成文字通道。</summary>
    public static ControlLink FromAccepted(
        FramedConnection conn,
        string peer,
        Action<string, long>? onText = null,
        Action<bool>? onState = null,
        Action<string, long, string?>? onPush = null,
        Action<string, long>? onNotice = null)
    {
        conn.SetTimeout(null);
        var link = new ControlLink(conn, peer, onText, onState, onPush, onNotice);
        link.Start();
        return link;
    }

    private void Start()
    {
        _reader.Start();
        _keepalive.Start();
    }

    private void ReadLoop()
    {
        try
        {
            while (!_closed)
            {
                var msg = _conn.RecvMsg();
                var cmd = msg.GetStr("cmd");
                if (cmd is null)
                {
                    // ack / pong
                    continue;
                }

                switch (cmd)
                {
                    case "say":
                    {
                        var text = msg.GetStr("text");
                        if (!string.IsNullOrEmpty(text))
                        {
                            _onText?.Invoke(text!, msg.GetLong("ts"));
                            Send(new Dictionary<string, object?>
                            {
                                ["ok"] = true,
                                ["ts"] = msg.TryGetValue("ts", out var ts) ? ts : null,
                            });
                        }
                        else
                        {
                            Send(new Dictionary<string, object?>
                            {
                                ["ok"] = false,
                                ["error"] = "text 必须是非空字符串",
                            });
                        }

                        break;
                    }

                    case "push":
                    {
                        string name;
                        long size;
                        try
                        {
                            name = FileNames.Sanitize(msg.GetStr("name"));
                            size = msg.RequireLong("size", "size");
                            if (size < 0 || size > ProtocolConst.MaxFileSize)
                            {
                                throw new ProtocolException("文件尺寸超出范围");
                            }
                        }
                        catch (ProtocolException ex)
                        {
                            Send(new Dictionary<string, object?> { ["ok"] = false, ["error"] = ex.Message });
                            continue;
                        }

                        var token = msg.GetStr("token");
                        _onPush?.Invoke(name, size, string.IsNullOrEmpty(token) ? null : token);
                        Send(new Dictionary<string, object?> { ["ok"] = true });
                        break;
                    }

                    case "notice":
                    {
                        var text = msg.GetStr("text");
                        if (!string.IsNullOrWhiteSpace(text))
                        {
                            _onNotice?.Invoke(text!, msg.GetLong("ts"));
                            Send(new Dictionary<string, object?> { ["ok"] = true });
                        }
                        else
                        {
                            Send(new Dictionary<string, object?>
                            {
                                ["ok"] = false,
                                ["error"] = "text 必须是非空字符串",
                            });
                        }

                        break;
                    }

                    case "ping":
                        Send(new Dictionary<string, object?> { ["ok"] = true, ["type"] = "pong" });
                        break;
                    case "bye":
                        return;
                    default:
                        Send(new Dictionary<string, object?>
                        {
                            ["ok"] = false,
                            ["error"] = $"未知命令: '{cmd}'",
                        });
                        break;
                }
            }
        }
        catch (Exception)
        {
            // 断开 / 协议错误都走统一的收尾
        }
        finally
        {
            var wasClosed = _closed;
            _closed = true;
            _conn.Dispose();
            if (!wasClosed && !_closing)
            {
                _onState?.Invoke(false);
            }
        }
    }

    private void PingLoop()
    {
        while (!_closed)
        {
            var waited = 0.0;
            while (waited < ProtocolConst.PingIntervalSeconds && !_closed)
            {
                Thread.Sleep(1000);
                waited += 1.0;
            }

            if (_closed)
            {
                return;
            }

            try
            {
                Send(new Dictionary<string, object?> { ["cmd"] = "ping" });
            }
            catch (Exception)
            {
                return;
            }
        }
    }

    private void Send(Dictionary<string, object?> message)
    {
        lock (_writeLock)
        {
            if (_closed)
            {
                throw new IOException("文字通道已关闭");
            }

            _conn.SendMsg(message);
        }
    }

    /// <summary>发送一条聊天消息。</summary>
    public bool SendText(string text)
    {
        text ??= string.Empty;
        if (string.IsNullOrWhiteSpace(text))
        {
            throw new ArgumentException("消息不能为空");
        }

        if (text.Length > ProtocolConst.MaxTextChars)
        {
            throw new ArgumentException($"单条消息最多 {ProtocolConst.MaxTextChars} 个字符");
        }

        Send(new Dictionary<string, object?>
        {
            ["cmd"] = "say",
            ["text"] = text,
            ["ts"] = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(),
        });
        return true;
    }

    /// <summary>通知对端来下载某个文件（token 可指向服务器本地任意路径）。</summary>
    public bool SendPush(string name, long size, string? token = null)
    {
        string clean;
        try
        {
            clean = FileNames.Sanitize(name);
            if (size < 0 || size > ProtocolConst.MaxFileSize)
            {
                throw new ProtocolException("文件尺寸超出范围");
            }
        }
        catch (ProtocolException ex)
        {
            throw new ArgumentException(ex.Message, ex);
        }

        var message = new Dictionary<string, object?>
        {
            ["cmd"] = "push",
            ["name"] = clean,
            ["size"] = size,
            ["ts"] = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(),
        };
        if (!string.IsNullOrEmpty(token))
        {
            message["token"] = token;
        }

        Send(message);
        return true;
    }

    /// <summary>发送一条系统通知（对端以灰色系统消息显示）。</summary>
    public bool SendNotice(string text)
    {
        var clean = (text ?? string.Empty).Trim();
        if (clean.Length == 0)
        {
            throw new ArgumentException("通知内容不能为空");
        }

        if (clean.Length > ProtocolConst.MaxTextChars)
        {
            clean = clean.Substring(0, ProtocolConst.MaxTextChars);
        }

        Send(new Dictionary<string, object?>
        {
            ["cmd"] = "notice",
            ["text"] = clean,
            ["ts"] = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(),
        });
        return true;
    }

    public void Close()
    {
        if (_closed)
        {
            return;
        }

        _closing = true;
        try
        {
            lock (_writeLock)
            {
                _conn.SendMsg(new Dictionary<string, object?> { ["cmd"] = "bye" });
            }
        }
        catch (Exception)
        {
            // 对端可能已经走了
        }

        _closed = true;
        _conn.Shutdown();
        _conn.Dispose();
    }

    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }

        _disposed = true;
        Close();
    }
}
