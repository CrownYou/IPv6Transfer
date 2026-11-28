using System;
using System.Diagnostics;
using System.Threading;

namespace Ipv6Transfer.Core.Protocol;

/// <summary>一次进度快照（对应 python 的 ProgressState.snapshot() 字典）。</summary>
public sealed class ProgressSnapshot
{
    public ProgressSnapshot(long done, long total, double fraction, double rate, double? eta, double elapsed)
    {
        Done = done;
        Total = total;
        Fraction = fraction;
        Rate = rate;
        Eta = eta;
        Elapsed = elapsed;
    }

    public long Done { get; }
    public long Total { get; }
    public double Fraction { get; }
    public double Rate { get; }
    public double? Eta { get; }
    public double Elapsed { get; }
}

/// <summary>记录字节进度并计算 EMA 速率与剩余时间。</summary>
public sealed class ProgressState
{
    private readonly long _startTicks;
    private long _lastTicks;
    private long _lastDone;
    private double _rate;

    public ProgressState(long total, long initial = 0)
    {
        Total = Math.Max(total, 0);
        Start = Math.Max(initial, 0);
        Done = Start;
        _startTicks = Stopwatch.GetTimestamp();
        _lastTicks = _startTicks;
        _lastDone = Done;
    }

    public long Total { get; }
    public long Start { get; private set; }
    public long Done { get; private set; }

    public void Update(long count) => Done += count;

    public ProgressSnapshot Snapshot()
    {
        var nowTicks = Stopwatch.GetTimestamp();
        var interval = (nowTicks - _lastTicks) / (double)Stopwatch.Frequency;
        if (interval > 0)
        {
            var instant = (Done - _lastDone) / interval;
            _rate = _rate <= 0 ? instant : 0.85 * _rate + 0.15 * instant;
            _lastTicks = nowTicks;
            _lastDone = Done;
        }

        var fraction = Total > 0 ? Done / (double)Total : 1.0;
        double? eta = null;
        if (_rate > 0 && Done < Total)
        {
            eta = (Total - Done) / _rate;
        }

        return new ProgressSnapshot(
            Done,
            Total,
            Math.Min(Math.Max(fraction, 0.0), 1.0),
            _rate,
            eta,
            (nowTicks - _startTicks) / (double)Stopwatch.Frequency);
    }

    /// <summary>收尾时补一帧 100% 进度，保证界面最后显示到完成。</summary>
    public ProgressSnapshot FinalSnapshot()
    {
        var snapshot = Snapshot();
        return new ProgressSnapshot(Total, Total, 1.0, snapshot.Rate, 0.0, snapshot.Elapsed);
    }
}

/// <summary>按平均速率限速，便于慢链路演示与复现续传。</summary>
public sealed class RateLimiter
{
    private readonly double _rate;
    private readonly long _startTicks;
    private long _sent;

    public RateLimiter(int bytesPerSecond)
    {
        _rate = bytesPerSecond > 0 ? bytesPerSecond : 0;
        _startTicks = Stopwatch.GetTimestamp();
    }

    public void Wait(long count)
    {
        if (_rate <= 0)
        {
            return;
        }

        _sent += count;
        var elapsed = (Stopwatch.GetTimestamp() - _startTicks) / (double)Stopwatch.Frequency;
        var delay = _sent / _rate - elapsed;
        if (delay > 0)
        {
            Thread.Sleep(TimeSpan.FromSeconds(Math.Min(delay, 1.0)));
        }
    }
}
