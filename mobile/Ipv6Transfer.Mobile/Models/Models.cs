using System;
using Ipv6Transfer.Core.Protocol;
using Microsoft.Maui.Graphics;

namespace Ipv6Transfer.Mobile.Models;

/// <summary>配色，与 python 版 main.py 的 COLORS 完全一致。</summary>
public static class AppColors
{
    public const string IdleHex = "#9AA0A6";
    public const string ListenHex = "#1A73E8";
    public const string OnlineHex = "#188038";
    public const string BusyHex = "#F29900";
    public const string ErrorHex = "#D93025";
    public const string MutedHex = "#5F6368";
    public const string OkHex = "#188038";
    public const string BadHex = "#D93025";
    public const string RunHex = "#F29900";
    public const string MeHex = "#1A73E8";
    public const string PeerHex = "#188038";
    public const string SysHex = "#5F6368";
    public const string PageBackground = "#F6F7F9";
    public const string CardBackground = "#FFFFFF";
    public const string BorderHex = "#DADCE0";

    public static Color Idle => Color.FromArgb(IdleHex);
    public static Color Listen => Color.FromArgb(ListenHex);
    public static Color Online => Color.FromArgb(OnlineHex);
    public static Color Busy => Color.FromArgb(BusyHex);
    public static Color Error => Color.FromArgb(ErrorHex);
    public static Color Muted => Color.FromArgb(MutedHex);
    public static Color Ok => Color.FromArgb(OkHex);
    public static Color Bad => Color.FromArgb(BadHex);
    public static Color Run => Color.FromArgb(RunHex);
    public static Color Me => Color.FromArgb(MeHex);
    public static Color Peer => Color.FromArgb(PeerHex);
    public static Color Sys => Color.FromArgb(SysHex);
}

/// <summary>传输记录里的一行。</summary>
public sealed class TransferRow : ViewModels.ObservableObject
{
    private string _name = "";
    private string _remote = "—";
    private string _direction = "";
    private string _status = "等待";
    private string _check = "—";
    private string _localSha256 = "";
    private string _peerSha256 = "";
    private bool? _matched;
    private long _size;
    private long _resumedFrom;
    private bool _finished;
    private string _serverPeer = "";
    private string _serverClientId = "";

    public string Id { get; init; } = "";

    public string Name
    {
        get => _name;
        set => Set(ref _name, value);
    }

    public string Remote
    {
        get => _remote;
        set => Set(ref _remote, value);
    }

    public string Direction
    {
        get => _direction;
        set => Set(ref _direction, value);
    }

    public string Status
    {
        get => _status;
        set
        {
            if (Set(ref _status, value))
            {
                Raise(nameof(StatusColor));
                Raise(nameof(CheckColor));
            }
        }
    }

    public string Check
    {
        get => _check;
        set => Set(ref _check, value);
    }

    public string LocalSha256
    {
        get => _localSha256;
        set => Set(ref _localSha256, value);
    }

    public string PeerSha256
    {
        get => _peerSha256;
        set => Set(ref _peerSha256, value);
    }

    public bool? Matched
    {
        get => _matched;
        set
        {
            if (Set(ref _matched, value))
            {
                Raise(nameof(StatusColor));
                Raise(nameof(CheckColor));
            }
        }
    }

    public long Size
    {
        get => _size;
        set
        {
            if (Set(ref _size, value))
            {
                Raise(nameof(SizeText));
            }
        }
    }

    public string SizeText => Formats.HumanBytes(_size);

    public long ResumedFrom
    {
        get => _resumedFrom;
        set => Set(ref _resumedFrom, value);
    }

    public bool Finished
    {
        get => _finished;
        set => Set(ref _finished, value);
    }

    public string ServerPeer
    {
        get => _serverPeer;
        set => Set(ref _serverPeer, value);
    }

    public string ServerClientId
    {
        get => _serverClientId;
        set => Set(ref _serverClientId, value);
    }

    public Color StatusColor => Status switch
    {
        "完成" => Matched == true ? AppColors.Ok : AppColors.Bad,
        "失败" => AppColors.Bad,
        "进行中" => AppColors.Run,
        _ => AppColors.Muted,
    };

    public Color CheckColor => Matched switch
    {
        true => AppColors.Ok,
        false => AppColors.Bad,
        _ => AppColors.Muted,
    };
}

/// <summary>发送页「待发送文件」里的一项。</summary>
public sealed class PendingFile
{
    public PendingFile(string path, long size)
    {
        Path = path;
        Size = size;
    }

    public string Path { get; }
    public long Size { get; }
    public string Name => System.IO.Path.GetFileName(Path);
    public string Display => $"{Name}  ({Formats.HumanBytes(Size)})";
}

/// <summary>在线客户端列表里的一项（服务器模式）。</summary>
public sealed class PeerEntry : ViewModels.ObservableObject
{
    private string _label = "";

    public PeerEntry(string key, string address, string label)
    {
        Key = key;
        Address = address;
        _label = label;
    }

    public string Key { get; }
    public string Address { get; }

    public string Label
    {
        get => _label;
        set => Set(ref _label, value);
    }
}

/// <summary>接收目录里的一个文件。</summary>
public sealed class FileEntry
{
    public required string Name { get; init; }
    public required string SizeText { get; init; }
    public required string MtimeText { get; init; }
    public required string FullPath { get; init; }
}

/// <summary>本机 IPv6 地址列表里的一项。</summary>
public sealed class AddressItem
{
    public required Ipv6AddressEntry Entry { get; init; }
    public required string Display { get; init; }
    public string Address => Entry.Address;
}

/// <summary>聊天消息的类别。</summary>
public enum ChatKind
{
    Me,
    Peer,
    System,
}

/// <summary>聊天页的一条消息。</summary>
public sealed class ChatEntry
{
    public required string Time { get; init; }
    public required string Who { get; init; }
    public required string Text { get; init; }
    public required ChatKind Kind { get; init; }

    public string Display => Kind == ChatKind.System
        ? $"[{Time}] 系统：{Text}"
        : $"[{Time}] {Who}：{Text}";

    public Color Color => Kind switch
    {
        ChatKind.Me => AppColors.Me,
        ChatKind.Peer => AppColors.Peer,
        _ => AppColors.Sys,
    };

    public LayoutOptions Alignment => Kind == ChatKind.Me
        ? LayoutOptions.End
        : LayoutOptions.Start;
}
