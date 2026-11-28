using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Security.Cryptography;
using System.Threading;

namespace Ipv6Transfer.Core.Protocol;

/// <summary>服务端会话向外抛的事件（start / progress / done / error）。</summary>
public sealed class TransferEventArgs : EventArgs
{
    public string Type { get; init; } = "";
    public string Direction { get; init; } = "";
    public string Name { get; init; } = "";
    public long Size { get; init; }
    public long ResumedFrom { get; init; }
    public long Done { get; init; }
    public long Total { get; init; }
    public double Fraction { get; init; }
    public double Rate { get; init; }
    public double? Eta { get; init; }
    public TransferResult? Result { get; init; }
    public string Error { get; init; } = "";
    public bool Fatal { get; init; }
    public string LocalSha256 { get; init; } = "";
    public string PeerSha256 { get; init; } = "";
    public bool Matched { get; init; }
}

/// <summary>按文件名加锁，避免两个客户端同时写同一个分片。</summary>
public sealed class NameLocks
{
    private readonly object _guard = new();
    private readonly Dictionary<string, SemaphoreSlim> _locks = new(StringComparer.Ordinal);

    public bool TryAcquire(string key)
    {
        SemaphoreSlim gate;
        lock (_guard)
        {
            if (!_locks.TryGetValue(key, out gate!))
            {
                gate = new SemaphoreSlim(1, 1);
                _locks[key] = gate;
            }
        }

        return gate.Wait(0);
    }

    public void Release(string key)
    {
        SemaphoreSlim? gate;
        lock (_guard)
        {
            _locks.TryGetValue(key, out gate);
        }

        if (gate is not null && gate.CurrentCount == 0)
        {
            try
            {
                gate.Release();
            }
            catch (SemaphoreFullException)
            {
                // 已经释放过
            }
        }
    }
}

/// <summary>
/// 服务端文件会话：处理 put / get / list / bye，直到对端关闭。
/// 对应 python 的 handle_transfer_session。
/// </summary>
public static class ServerSession
{
    public static void HandleTransferSession(
        FramedConnection conn,
        string root,
        double timeoutSeconds = 120.0,
        NameLocks? locks = null,
        Action<TransferEventArgs>? emit = null,
        Func<string, string?>? pathResolver = null)
    {
        locks ??= new NameLocks();
        conn.SetTimeout((int)Math.Round(timeoutSeconds * 1000));
        while (true)
        {
            var msg = conn.RecvMsg();
            var cmd = msg.GetStr("cmd") ?? string.Empty;
            switch (cmd)
            {
                case "put":
                    ServePut(conn, msg, root, locks, emit);
                    break;
                case "get":
                    ServeGet(conn, msg, root, emit, pathResolver);
                    break;
                case "list":
                    ServeList(conn, root);
                    break;
                case "bye":
                    conn.SendMsg(new Dictionary<string, object?> { ["ok"] = true });
                    return;
                default:
                    conn.SendMsg(new Dictionary<string, object?>
                    {
                        ["ok"] = false,
                        ["error"] = $"未知命令: '{cmd}'",
                    });
                    return;
            }
        }
    }

    private static void ServePut(FramedConnection conn, Dictionary<string, object?> msg, string root, NameLocks locks, Action<TransferEventArgs>? emit)
    {
        var name = FileNames.Sanitize(msg.GetStr("name"));
        var size = msg.RequireLong("size", "size");
        if (size < 0 || size > ProtocolConst.MaxFileSize)
        {
            throw new ProtocolException("文件尺寸超出范围");
        }

        var resumeRequested = msg.GetBool("resume", true);
        var key = name.ToLowerInvariant();
        if (!locks.TryAcquire(key))
        {
            conn.SendMsg(new Dictionary<string, object?>
            {
                ["ok"] = false,
                ["error"] = $"文件 {name} 正在被其他客户端传输",
            });
            emit?.Invoke(new TransferEventArgs
            {
                Type = "error",
                Direction = "receive",
                Name = name,
                Error = "同名文件正在被其他客户端传输",
            });
            return;
        }

        try
        {
            var part = FileNames.PartPath(root, name);
            var existing = File.Exists(part) ? new FileInfo(part).Length : 0;
            if (!resumeRequested || existing > size)
            {
                if (File.Exists(part))
                {
                    using var fp = new FileStream(part, FileMode.Open, FileAccess.Write, FileShare.Read);
                    fp.SetLength(0);
                }

                existing = 0;
            }

            var start = Math.Min(existing, size);
            conn.SendMsg(new Dictionary<string, object?>
            {
                ["ok"] = true,
                ["resume_from"] = start,
                ["size"] = size,
                ["name"] = name,
            });
            emit?.Invoke(new TransferEventArgs
            {
                Type = "start",
                Direction = "receive",
                Name = name,
                Size = size,
                ResumedFrom = start,
            });

            var began = Stopwatch.GetTimestamp();
            using var hasher = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
            if (start > 0)
            {
                using var prefix = new FileStream(part, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
                FileTransfer.FeedPrefix(prefix, hasher, start);
            }

            var progress = new ProgressState(size, start);
            var sink = FileTransfer.MakeSink(
                hasher,
                progress,
                snapshot => EmitProgress(emit, "receive", name, snapshot),
                null,
                null);
            using (var fp = new FileStream(part, FileMode.Append, FileAccess.Write, FileShare.Read))
            {
                conn.RecvToStream(fp, size - start, sink);
            }

            var done = conn.RecvMsg();
            if (done.GetStr("cmd") != "done")
            {
                throw new ProtocolException("缺少 done 消息");
            }

            var claimed = (done.GetStr("sha256") ?? string.Empty).ToLowerInvariant();
            if (claimed.Length != 64 || !IsHex(claimed))
            {
                throw new ProtocolException("done.sha256 不是合法十六进制摘要");
            }

            var localHash = FileTransfer.HashToHex(hasher);
            var actualSize = File.Exists(part) ? new FileInfo(part).Length : -1;
            var elapsed = Math.Max(FileTransfer.SecondsSince(began), 1e-6);
            if (actualSize != size || localHash != claimed)
            {
                FileTransfer.TryDelete(part);
                conn.SendMsg(new Dictionary<string, object?>
                {
                    ["ok"] = false,
                    ["error"] = "hash_mismatch",
                    ["restart"] = true,
                    ["expected"] = claimed,
                    ["actual"] = localHash,
                    ["received_size"] = actualSize,
                });
                emit?.Invoke(new TransferEventArgs
                {
                    Type = "error",
                    Direction = "receive",
                    Name = name,
                    Error = $"完整性校验失败（收到 {actualSize}/{size} 字节），已删除分片",
                    LocalSha256 = localHash,
                    PeerSha256 = claimed,
                    Matched = false,
                });
                return;
            }

            var final = FileNames.UniqueFinalName(root, name);
            FileNames.EnsureInside(root, final);
            File.Move(part, final, true);
            conn.SendMsg(new Dictionary<string, object?>
            {
                ["ok"] = true,
                ["name"] = Path.GetFileName(final),
                ["size"] = size,
                ["sha256"] = localHash,
            });
            emit?.Invoke(new TransferEventArgs
            {
                Type = "done",
                Direction = "receive",
                Name = name,
                Result = new TransferResult
                {
                    Ok = true,
                    Direction = "receive",
                    Name = name,
                    FinalName = Path.GetFileName(final),
                    Path = System.IO.Path.GetFullPath(final),
                    Size = size,
                    LocalSha256 = localHash,
                    PeerSha256 = claimed,
                    Matched = localHash == claimed,
                    ResumedFrom = start,
                    Seconds = elapsed,
                },
            });
        }
        finally
        {
            locks.Release(key);
        }
    }

    private static void ServeGet(
        FramedConnection conn,
        Dictionary<string, object?> msg,
        string root,
        Action<TransferEventArgs>? emit,
        Func<string, string?>? pathResolver)
    {
        var name = FileNames.Sanitize(msg.GetStr("name"));
        var token = msg.GetStr("token");
        string? resolved = null;
        if (!string.IsNullOrEmpty(token) && pathResolver is not null)
        {
            try
            {
                resolved = pathResolver(token!);
            }
            catch (Exception)
            {
                resolved = null;
            }
        }

        var path = !string.IsNullOrEmpty(resolved)
            ? resolved!
            : FileNames.EnsureInside(root, Path.Combine(root, name));

        if (!File.Exists(path))
        {
            conn.SendMsg(new Dictionary<string, object?>
            {
                ["ok"] = false,
                ["error"] = $"服务器上不存在文件 {name}",
            });
            emit?.Invoke(new TransferEventArgs
            {
                Type = "error",
                Direction = "send",
                Name = name,
                Error = "文件不存在",
            });
            return;
        }

        var size = new FileInfo(path).Length;
        var offset = msg.GetLong("offset", 0);
        var start = offset > 0 && offset <= size ? offset : 0;
        var digest = Sha256OfFile(path);

        conn.SendMsg(new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["name"] = name,
            ["size"] = size,
            ["sha256"] = digest,
            ["resume_from"] = start,
        });
        emit?.Invoke(new TransferEventArgs
        {
            Type = "start",
            Direction = "send",
            Name = name,
            Size = size,
            ResumedFrom = start,
        });

        var began = Stopwatch.GetTimestamp();
        var progress = new ProgressState(size, start);
        var sink = FileTransfer.MakeSink(
            null,
            progress,
            snapshot => EmitProgress(emit, "send", name, snapshot),
            null,
            null);
        using (var fp = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
        {
            fp.Seek(start, SeekOrigin.Begin);
            conn.SendFromStream(fp, size - start, sink);
        }

        var ack = conn.RecvMsg();
        var elapsed = Math.Max(FileTransfer.SecondsSince(began), 1e-6);
        var peerHash = (ack.GetStr("sha256") ?? string.Empty).ToLowerInvariant();
        var matched = ack.GetBool("ok") && peerHash == digest;
        if (ack.GetBool("ok"))
        {
            emit?.Invoke(new TransferEventArgs
            {
                Type = "done",
                Direction = "send",
                Name = name,
                Result = new TransferResult
                {
                    Ok = true,
                    Direction = "send",
                    Name = name,
                    FinalName = name,
                    Path = System.IO.Path.GetFullPath(path),
                    Size = size,
                    LocalSha256 = digest,
                    PeerSha256 = peerHash,
                    Matched = matched,
                    ResumedFrom = start,
                    Seconds = elapsed,
                },
            });
        }
        else
        {
            emit?.Invoke(new TransferEventArgs
            {
                Type = "error",
                Direction = "send",
                Name = name,
                Error = "客户端报告校验失败",
                LocalSha256 = digest,
                PeerSha256 = peerHash,
                Matched = false,
            });
        }
    }

    private static void ServeList(FramedConnection conn, string root)
    {
        var files = new List<Dictionary<string, object?>>();
        try
        {
            foreach (var path in Directory.EnumerateFiles(root))
            {
                var fileName = Path.GetFileName(path);
                if (fileName.EndsWith(".part", StringComparison.Ordinal))
                {
                    continue;
                }

                FileInfo info;
                try
                {
                    info = new FileInfo(path);
                    if (!info.Exists)
                    {
                        continue;
                    }
                }
                catch (Exception)
                {
                    continue;
                }

                files.Add(new Dictionary<string, object?>
                {
                    ["name"] = fileName,
                    ["size"] = info.Length,
                    ["mtime"] = new DateTimeOffset(info.LastWriteTimeUtc).ToUnixTimeSeconds(),
                });
                if (files.Count >= ProtocolConst.MaxListEntries)
                {
                    break;
                }
            }
        }
        catch (Exception ex)
        {
            conn.SendMsg(new Dictionary<string, object?>
            {
                ["ok"] = false,
                ["error"] = $"读取目录失败：{ex.Message}",
            });
            return;
        }

        files.Sort((a, b) => string.Compare(
            a["name"] as string,
            b["name"] as string,
            StringComparison.CurrentCultureIgnoreCase));
        conn.SendMsg(new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["files"] = files,
            ["root"] = System.IO.Path.GetFullPath(root),
        });
    }

    private static void EmitProgress(Action<TransferEventArgs>? emit, string direction, string name, ProgressSnapshot snapshot)
    {
        emit?.Invoke(new TransferEventArgs
        {
            Type = "progress",
            Direction = direction,
            Name = name,
            Done = snapshot.Done,
            Total = snapshot.Total,
            Fraction = snapshot.Fraction,
            Rate = snapshot.Rate,
            Eta = snapshot.Eta,
        });
    }

    private static bool IsHex(string text)
    {
        foreach (var ch in text)
        {
            if (!Uri.IsHexDigit(ch))
            {
                return false;
            }
        }

        return true;
    }

    public static string Sha256OfFile(string path)
    {
        using var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
        using var sha = SHA256.Create();
        return Convert.ToHexString(sha.ComputeHash(stream)).ToLowerInvariant();
    }
}
