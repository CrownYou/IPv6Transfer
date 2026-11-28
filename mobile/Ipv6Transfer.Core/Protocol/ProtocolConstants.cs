using System;
using System.Collections.Generic;

namespace Ipv6Transfer.Core.Protocol;

/// <summary>
/// 协议常量，与 python/file_core.py 保持一致。改动任意一个都可能和电脑版不兼容。
/// </summary>
public static class ProtocolConst
{
    public const int ProtoVersion = 1;
    public const int DefaultPort = 9000;
    public const string AuthMethod = "hmac-sha256";
    public const string ConfigPrefix = "FT6";
    public const int MaxPasswordChars = 256;

    /// <summary>1 MiB：流式收发，内存占用恒定。</summary>
    public const int ChunkSize = 1 << 20;

    /// <summary>单条控制消息上限（64 KiB）。</summary>
    public const int MaxLine = 64 * 1024;

    /// <summary>1 TiB。</summary>
    public const long MaxFileSize = 1L << 40;

    public const int MaxNameBytes = 200;
    public const int MaxListEntries = 10000;
    public const int MaxTextChars = 4000;

    public const double PingIntervalSeconds = 30.0;

    /// <summary>进度回调最短间隔（秒）。</summary>
    public const double EmitIntervalSeconds = 0.1;
}

/// <summary>对端数据不符合协议约定。</summary>
public class ProtocolException : Exception
{
    public ProtocolException(string message) : base(message) { }
    public ProtocolException(string message, Exception inner) : base(message, inner) { }
}

/// <summary>握手失败，且已经回送过错误应答。</summary>
public class HelloRejectedException : ProtocolException
{
    public HelloRejectedException(string message) : base(message) { }
}

/// <summary>无法建立到对端的连接。</summary>
public class ConnectException : Exception
{
    public ConnectException(string message) : base(message) { }
    public ConnectException(string message, Exception inner) : base(message, inner) { }
}

/// <summary>用户取消了当前传输。</summary>
public class TransferCancelledException : Exception
{
    public TransferCancelledException(string message = "用户已取消") : base(message) { }
}

/// <summary>连接已关闭 / 传输中断类的可重试错误集合。</summary>
public static class ErrorKinds
{
    /// <summary>
    /// python 侧把这些都归在 (OSError, ConnectionError, ProtocolError) 一起处理，
    /// 这里保持一致：SocketException / IOException / ProtocolException 都算传输层错误。
    /// </summary>
    public static bool IsTransportError(Exception ex)
        => ex is System.Net.Sockets.SocketException
            or System.IO.IOException
            or ProtocolException
            or ObjectDisposedException;
}
