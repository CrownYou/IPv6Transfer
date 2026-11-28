using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Security.Cryptography;

namespace Ipv6Transfer.Core.Protocol;

/// <summary>单个文件的传输结果（对应 python 的 TransferResult）。</summary>
public sealed class TransferResult
{
    public bool Ok { get; init; }
    public string Direction { get; init; } = "";
    public string Name { get; init; } = "";
    public string FinalName { get; init; } = "";
    public string Path { get; init; } = "";
    public long Size { get; init; }
    public string LocalSha256 { get; init; } = "";
    public string PeerSha256 { get; init; } = "";
    public bool Matched { get; init; }
    public long ResumedFrom { get; init; }
    public double Seconds { get; init; }
    public string Error { get; init; } = "";
    public bool Retried { get; init; }

    /// <summary>服务端 emit 时需要把结果当作普通字典发出去，键名与 python 一致。</summary>
    public Dictionary<string, object?> ToData() => new()
    {
        ["ok"] = Ok,
        ["direction"] = Direction,
        ["name"] = Name,
        ["final_name"] = FinalName,
        ["path"] = Path,
        ["size"] = Size,
        ["local_sha256"] = LocalSha256,
        ["peer_sha256"] = PeerSha256,
        ["matched"] = Matched,
        ["resumed_from"] = ResumedFrom,
        ["seconds"] = Seconds,
        ["error"] = Error,
        ["retried"] = Retried,
    };
}

/// <summary>传输阶段变化（开始 / 校验不一致重传）。</summary>
public sealed record TransferStageInfo(string Stage, string Name, long Size, long ResumedFrom, string? Error = null);

/// <summary>对端目录里的一个文件。</summary>
public sealed record RemoteFile(string Name, long Size, long Mtime);

/// <summary>
/// 客户端视角的单文件传输：上传 / 下载 / 列目录。
/// 全部是阻塞式实现（与 python 版一一对应），由界面层放到后台线程执行。
/// </summary>
public static class FileTransfer
{
    /// <summary>上传一个本地文件；返回含双方哈希的结果。</summary>
    public static TransferResult SendFile(
        FramedConnection conn,
        string path,
        bool resume = true,
        Action<ProgressSnapshot>? onProgress = null,
        Action<TransferStageInfo>? onStage = null,
        Func<bool>? shouldCancel = null,
        int limitRate = 0,
        string? name = null)
    {
        var size = new FileInfo(path).Length;
        name ??= Path.GetFileName(path);
        var limiter = limitRate > 0 ? new RateLimiter(limitRate) : null;
        var retried = false;

        for (var attempt = 1; attempt <= 2; attempt++)
        {
            var began = Stopwatch.GetTimestamp();
            conn.SendMsg(new Dictionary<string, object?>
            {
                ["cmd"] = "put",
                ["name"] = name,
                ["size"] = size,
                ["resume"] = resume,
            });
            var reply = conn.RecvMsg();
            if (!reply.GetBool("ok"))
            {
                return new TransferResult
                {
                    Ok = false,
                    Direction = "upload",
                    Name = name,
                    Size = size,
                    Error = reply.GetStr("error") ?? "对端拒绝了上传",
                };
            }

            var start = reply.GetLong("resume_from", 0);
            if (start < 0 || start > size)
            {
                return new TransferResult
                {
                    Ok = false,
                    Direction = "upload",
                    Name = name,
                    Size = size,
                    Error = $"对端返回的续传偏移不合法（{start}）",
                };
            }

            onStage?.Invoke(new TransferStageInfo("start", name, size, start));

            using var hasher = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
            var progress = new ProgressState(size, start);
            var sink = MakeSink(hasher, progress, onProgress, shouldCancel, limiter);
            try
            {
                using var fp = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read);
                if (start > 0)
                {
                    FeedPrefix(fp, hasher, start);
                }

                fp.Seek(start, SeekOrigin.Begin);
                conn.SendFromStream(fp, size - start, sink);
            }
            catch (TransferCancelledException)
            {
                return new TransferResult
                {
                    Ok = false,
                    Direction = "upload",
                    Name = name,
                    Size = size,
                    ResumedFrom = start,
                    Error = "已取消",
                    LocalSha256 = HashToHex(hasher),
                };
            }

            var localHash = HashToHex(hasher);
            conn.SendMsg(new Dictionary<string, object?> { ["cmd"] = "done", ["sha256"] = localHash });
            reply = conn.RecvMsg();
            var elapsed = Math.Max(SecondsSince(began), 1e-6);
            if (reply.GetBool("ok"))
            {
                onProgress?.Invoke(progress.FinalSnapshot());
                var peerHash = (reply.GetStr("sha256") ?? string.Empty).ToLowerInvariant();
                return new TransferResult
                {
                    Ok = true,
                    Direction = "upload",
                    Name = name,
                    FinalName = reply.GetStr("name") ?? name,
                    Path = System.IO.Path.GetFullPath(path),
                    Size = size,
                    LocalSha256 = localHash,
                    PeerSha256 = peerHash,
                    Matched = peerHash.Length > 0 && peerHash == localHash,
                    ResumedFrom = start,
                    Seconds = elapsed,
                    Retried = retried,
                };
            }

            var error = reply.GetStr("error") ?? "上传失败";
            if (reply.GetBool("restart") && attempt == 1)
            {
                retried = true;
                onStage?.Invoke(new TransferStageInfo("restart", name, size, 0, error));
                continue;
            }

            return new TransferResult
            {
                Ok = false,
                Direction = "upload",
                Name = name,
                Size = size,
                ResumedFrom = start,
                LocalSha256 = localHash,
                PeerSha256 = (reply.GetStr("actual") ?? string.Empty).ToLowerInvariant(),
                Seconds = elapsed,
                Error = error,
                Retried = retried,
            };
        }

        return new TransferResult { Ok = false, Direction = "upload", Name = name, Size = size, Error = "上传失败" };
    }

    /// <summary>
    /// 从对端下载一个文件到 outDir；token 用于服务器主动推送的任意路径文件。
    /// </summary>
    public static TransferResult RecvFile(
        FramedConnection conn,
        string name,
        string outDir,
        bool resume = true,
        Action<ProgressSnapshot>? onProgress = null,
        Action<TransferStageInfo>? onStage = null,
        Func<bool>? shouldCancel = null,
        int limitRate = 0,
        string? token = null)
    {
        Directory.CreateDirectory(outDir);
        var baseName = FileNames.Sanitize(name);
        var part = System.IO.Path.Combine(outDir, baseName + ".part");
        var limiter = limitRate > 0 ? new RateLimiter(limitRate) : null;
        var retried = false;

        for (var attempt = 1; attempt <= 2; attempt++)
        {
            var began = Stopwatch.GetTimestamp();
            var offset = resume && File.Exists(part) ? new FileInfo(part).Length : 0;
            var request = new Dictionary<string, object?>
            {
                ["cmd"] = "get",
                ["name"] = baseName,
                ["offset"] = offset,
            };
            if (!string.IsNullOrEmpty(token))
            {
                request["token"] = token;
            }

            conn.SendMsg(request);
            var reply = conn.RecvMsg();
            if (!reply.GetBool("ok"))
            {
                return new TransferResult
                {
                    Ok = false,
                    Direction = "download",
                    Name = baseName,
                    Error = reply.GetStr("error") ?? "下载失败",
                };
            }

            var size = reply.GetLong("size", -1);
            if (size < 0)
            {
                return new TransferResult
                {
                    Ok = false,
                    Direction = "download",
                    Name = baseName,
                    Error = "对端返回的文件尺寸不合法",
                };
            }

            var peerHash = (reply.GetStr("sha256") ?? string.Empty).ToLowerInvariant();
            var start = reply.GetLong("resume_from", 0);
            if (start < 0 || start > size)
            {
                start = 0;
            }

            using var hasher = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
            if (File.Exists(part))
            {
                if (start > 0)
                {
                    using (var prefix = new FileStream(part, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
                    {
                        FeedPrefix(prefix, hasher, start);
                    }

                    using var truncate = new FileStream(part, FileMode.Open, FileAccess.Write, FileShare.Read);
                    truncate.SetLength(start);
                }
                else
                {
                    using var truncate = new FileStream(part, FileMode.Open, FileAccess.Write, FileShare.Read);
                    truncate.SetLength(0);
                }
            }
            else if (start > 0)
            {
                start = 0;
            }

            onStage?.Invoke(new TransferStageInfo("start", baseName, size, start));

            var progress = new ProgressState(size, start);
            var sink = MakeSink(hasher, progress, onProgress, shouldCancel, limiter);
            try
            {
                using var fp = new FileStream(part, FileMode.Append, FileAccess.Write, FileShare.Read);
                conn.RecvToStream(fp, size - start, sink);
            }
            catch (TransferCancelledException)
            {
                return new TransferResult
                {
                    Ok = false,
                    Direction = "download",
                    Name = baseName,
                    Size = size,
                    ResumedFrom = start,
                    Error = "已取消",
                    LocalSha256 = HashToHex(hasher),
                };
            }

            var localHash = HashToHex(hasher);
            var actualSize = File.Exists(part) ? new FileInfo(part).Length : -1;
            var ok = actualSize == size && localHash == peerHash;
            conn.SendMsg(new Dictionary<string, object?>
            {
                ["cmd"] = "done",
                ["ok"] = ok,
                ["sha256"] = localHash,
            });
            var elapsed = Math.Max(SecondsSince(began), 1e-6);

            if (ok)
            {
                onProgress?.Invoke(progress.FinalSnapshot());
                var final = FileNames.UniqueFinalName(outDir, baseName);
                File.Move(part, final, true);
                return new TransferResult
                {
                    Ok = true,
                    Direction = "download",
                    Name = baseName,
                    FinalName = System.IO.Path.GetFileName(final),
                    Path = System.IO.Path.GetFullPath(final),
                    Size = size,
                    LocalSha256 = localHash,
                    PeerSha256 = peerHash,
                    Matched = true,
                    ResumedFrom = start,
                    Seconds = elapsed,
                    Retried = retried,
                };
            }

            TryDelete(part);
            if (attempt == 1)
            {
                retried = true;
                onStage?.Invoke(new TransferStageInfo("restart", baseName, size, 0, "hash_mismatch"));
                continue;
            }

            return new TransferResult
            {
                Ok = false,
                Direction = "download",
                Name = baseName,
                Size = size,
                ResumedFrom = start,
                LocalSha256 = localHash,
                PeerSha256 = peerHash,
                Seconds = elapsed,
                Error = "完整性校验不一致",
                Retried = retried,
            };
        }

        return new TransferResult { Ok = false, Direction = "download", Name = baseName, Error = "下载失败" };
    }

    /// <summary>向服务端要一份目录清单。</summary>
    public static (List<RemoteFile> Files, string Root) ListRemote(FramedConnection conn)
    {
        conn.SendMsg(new Dictionary<string, object?> { ["cmd"] = "list" });
        var reply = conn.RecvMsg();
        if (!reply.GetBool("ok"))
        {
            throw new ProtocolException(reply.GetStr("error") ?? "获取列表失败");
        }

        var files = new List<RemoteFile>();
        foreach (var item in reply.GetArray("files") ?? new List<object?>())
        {
            if (item is Dictionary<string, object?> entry)
            {
                files.Add(new RemoteFile(
                    entry.GetStr("name") ?? string.Empty,
                    entry.GetLong("size"),
                    entry.GetLong("mtime")));
            }
        }

        return (files, reply.GetStr("root") ?? string.Empty);
    }

    // ------------------------------------------------------------------ #
    // 内部工具
    // ------------------------------------------------------------------ #

    /// <summary>构造 per-chunk 回调：取消检查 + 哈希 + 进度 + 限速 + 限频上报。</summary>
    internal static Action<ReadOnlySpan<byte>> MakeSink(
        IncrementalHash? hasher,
        ProgressState? progress,
        Action<ProgressSnapshot>? onProgress,
        Func<bool>? shouldCancel,
        RateLimiter? limiter,
        double interval = ProtocolConst.EmitIntervalSeconds)
    {
        var lastReport = double.NegativeInfinity;
        return chunk =>
        {
            if (shouldCancel is not null && shouldCancel())
            {
                throw new TransferCancelledException();
            }

            hasher?.AppendData(chunk);
            progress?.Update(chunk.Length);
            limiter?.Wait(chunk.Length);

            if (onProgress is not null && progress is not null)
            {
                var now = NowSeconds();
                if (now - lastReport >= interval)
                {
                    lastReport = now;
                    onProgress(progress.Snapshot());
                }
            }
        };
    }

    /// <summary>把 fp 开头 count 字节喂给哈希器（续传时补算已有前缀）。</summary>
    internal static void FeedPrefix(Stream fp, IncrementalHash hasher, long count)
    {
        var remaining = count;
        var buffer = new byte[(int)Math.Min(ProtocolConst.ChunkSize, Math.Max(remaining, 1))];
        while (remaining > 0)
        {
            var want = (int)Math.Min(buffer.Length, remaining);
            var got = fp.Read(buffer, 0, want);
            if (got <= 0)
            {
                throw new ProtocolException("分片文件比预期短");
            }

            hasher.AppendData(buffer, 0, got);
            remaining -= got;
        }
    }

    internal static string HashToHex(IncrementalHash hasher)
        => Convert.ToHexString(hasher.GetHashAndReset()).ToLowerInvariant();

    internal static double NowSeconds() => Stopwatch.GetTimestamp() / (double)Stopwatch.Frequency;

    internal static double SecondsSince(long startTicks) => (Stopwatch.GetTimestamp() - startTicks) / (double)Stopwatch.Frequency;

    internal static void TryDelete(string path)
    {
        try
        {
            if (File.Exists(path))
            {
                File.Delete(path);
            }
        }
        catch (Exception)
        {
            // 删不掉就算了，后续会再覆盖
        }
    }
}
