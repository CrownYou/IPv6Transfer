using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.IO;
using System.Linq;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Input;
using Ipv6Transfer.Core.Protocol;
using Ipv6Transfer.Mobile.Models;
using Ipv6Transfer.Mobile.Services;
using Microsoft.Maui.ApplicationModel;
using Microsoft.Maui.Graphics;
using Microsoft.Maui.Storage;

namespace Ipv6Transfer.Mobile.ViewModels;

/// <summary>
/// 手机版主界面的全部状态与逻辑，逐项对应 python/main.py 的 MobileApp：
/// 服务器监听、客户端连接、待发送队列、上传/下载、推送、聊天、别名、配置串。
/// </summary>
public sealed class MainViewModel : ObservableObject, IDisposable
{
    private const string StateIdle = "idle";
    private const string StateConnecting = "connecting";
    private const string StateListening = "listening";
    private const string StateConnected = "connected";
    private const string StateFileOnly = "file_only";
    private const string StateBusy = "busy";

    private const int MaxUiFiles = 300;
    private const int AutoRefreshSeconds = 3;

    private readonly IDialogService _dialogs;
    private readonly AppConfig _config;

    // -- 连接态 ---------------------------------------------------------
    private readonly Dictionary<string, ControlLink> _controls = new(StringComparer.Ordinal);
    private readonly Dictionary<string, string> _controlPeers = new(StringComparer.Ordinal);
    private readonly Dictionary<string, string> _clientAliases = new(StringComparer.Ordinal);
    private readonly Dictionary<string, string> _pushRegistry = new(StringComparer.Ordinal);
    private readonly List<(string Name, string? Token)> _pushQueue = new();
    private readonly Dictionary<string, TransferRow> _rows = new(StringComparer.Ordinal);
    private readonly Dictionary<string, string> _serverRowIds = new(StringComparer.Ordinal);
    private readonly List<string> _peerKeys = new();
    private readonly NameLocks _nameLocks = new();

    private Socket? _listener;
    private CancellationTokenSource? _acceptCts;
    private CancellationTokenSource? _transferCts;
    private CancellationTokenSource? _autoRefreshCts;
    private Socket? _transferSocket;
    private string _state = StateIdle;
    private string _peerAddress = "";
    private bool _fileOnlyMode;
    private long _rowSequence;
    private string _recvSignature = "";
    private bool _disposed;

    // -- 绑定字段 -------------------------------------------------------
    private string _role;
    private string _port;
    private string _host;
    private string _zone;
    private string _password;
    private string _serverRecvDir;
    private string _clientRecvDir;
    private string _statusText = "未连接";
    private Color _statusColor = AppColors.Idle;
    private string _peerText = "";
    private string _hintText = "";
    private string _pendingSummary = "还没有选择文件";
    private string _taskText = "空闲";
    private string _statsText = "等待任务";
    private double _progress;
    private string _chatInput = "";
    private string _chatTitle = "聊天";
    private string _receiveDirLabel = "";
    private bool _isConnectTab = true;
    private bool _isSendTab;
    private bool _isReceiveTab;
    private bool _isChatTab;
    private PeerEntry? _selectedPeer;
    private AddressItem? _selectedAddress;
    private PendingFile? _selectedPending;
    private TransferRow? _selectedRow;
    private bool _showStorageWarning;
    private string _storageWarningText = "";
    private bool _canStartServer;
    private bool _canStopServer;
    private bool _canConnect;
    private bool _canDisconnect;
    private bool _canSend;
    private bool _canCancel;
    private bool _canChat;
    private bool _canSetAlias;
    private bool _canCopyAddress;

    public MainViewModel(IDialogService dialogs)
    {
        _dialogs = dialogs;
        _config = AppConfig.Load();
        ClientId = Guid.NewGuid().ToString("N")[..12];

        _role = _config.Role == "server" ? "server" : "client";
        _port = string.IsNullOrWhiteSpace(_config.Port) ? "9000" : _config.Port.Trim();
        _host = string.IsNullOrWhiteSpace(_config.Host) ? "::1" : _config.Host.Trim();
        _zone = _config.Zone?.Trim() ?? "";
        _password = _config.Password ?? "";

        foreach (var pair in _config.ClientAliases)
        {
            if (!string.IsNullOrWhiteSpace(pair.Value))
            {
                _clientAliases[pair.Key] = pair.Value.Trim();
            }
        }

        _serverRecvDir = string.IsNullOrWhiteSpace(_config.RecvDir)
            ? MobileStorage.DefaultDirectory("received")
            : _config.RecvDir;
        _clientRecvDir = string.IsNullOrWhiteSpace(_config.ClientRecvDir)
            ? MobileStorage.DefaultDirectory("downloads")
            : _config.ClientRecvDir;

        SelectTabCommand = new RelayCommand(parameter => SelectTab(parameter as string ?? "conn"));
        SelectRoleCommand = new RelayCommand(parameter => SelectRole(parameter as string ?? "client"));
        StartServerCommand = new RelayCommand(_ => StartServer(), _ => CanStartServer);
        StopServerCommand = new RelayCommand(_ => StopServer(), _ => CanStopServer);
        ConnectCommand = new RelayCommand(_ => ConnectPeer(), _ => CanConnect);
        DisconnectCommand = new RelayCommand(_ => DisconnectPeer(), _ => CanDisconnect);
        AddFilesCommand = new AsyncRelayCommand(AddFilesAsync);
        StartSendCommand = new RelayCommand(_ => StartSend(), _ => CanSend);
        CancelCommand = new RelayCommand(_ => CancelTransfer(), _ => CanCancel);
        RemovePendingCommand = new RelayCommand(_ => RemoveSelectedPending());
        ClearPendingCommand = new RelayCommand(_ => ClearPending());
        BrowseServerDirCommand = new AsyncRelayCommand(BrowseServerDirAsync);
        BrowseClientDirCommand = new AsyncRelayCommand(BrowseClientDirAsync);
        CopyAddressCommand = new AsyncRelayCommand(CopyAddressAsync, () => CanCopyAddress);
        RefreshAddressCommand = new RelayCommand(_ => RefreshAddresses());
        ConfigCommand = new AsyncRelayCommand(ToggleConnectionConfigAsync);
        SetAliasCommand = new AsyncRelayCommand(SetAliasAsync, () => CanSetAlias);
        CopyReceivePathCommand = new AsyncRelayCommand(CopyReceivePathAsync);
        RefreshReceiveListCommand = new RelayCommand(_ => RefreshLocalList(true));
        ShowRowDetailCommand = new AsyncRelayCommand(ShowRowDetailAsync);
        ShowFileDetailCommand = new AsyncRelayCommand(ShowFileDetailAsync);
        SendChatCommand = new AsyncRelayCommand(SendChatAsync, () => CanChat);
        RequestStorageCommand = new AsyncRelayCommand(RequestStorageAsync);


        Log("手机版已就绪：选「服务器」点启动监听，或选「客户端」填地址后连接。");
        RefreshStorageWarning();
        SetState(StateIdle);
        RefreshAddresses();
        RefreshLocalList(true);
        RefreshPendingView();
        StartAutoRefresh();
    }

    // ------------------------------------------------------------------ #
    // 基础属性
    // ------------------------------------------------------------------ #

    public string ClientId { get; }

    public string AppTitle => "IPv6 文件传输";

    public ObservableCollection<PeerEntry> Peers { get; } = new();
    public ObservableCollection<AddressItem> Addresses { get; } = new();
    public ObservableCollection<PendingFile> PendingFiles { get; } = new();
    public ObservableCollection<TransferRow> History { get; } = new();
    public ObservableCollection<FileEntry> ReceivedFiles { get; } = new();
    public ObservableCollection<ChatEntry> Chat { get; } = new();

    /// <summary>聊天有未读消息时置位，界面用它滚动到底部。</summary>
    public event EventHandler? ChatAppended;

    public string Role
    {
        get => _role;
        private set
        {
            if (!Set(ref _role, value))
            {
                return;
            }

            Raise(nameof(IsServerRole), nameof(IsClientRole), nameof(IsServerPanelVisible),
                nameof(IsClientPanelVisible), nameof(ConfigButtonText), nameof(SendButtonText));
        }
    }

    public bool IsServerRole
    {
        get => _role == "server";
        set
        {
            if (value)
            {
                _ = ChangeRoleAsync("server");
            }
        }
    }

    public bool IsClientRole
    {
        get => _role == "client";
        set
        {
            if (value)
            {
                _ = ChangeRoleAsync("client");
            }
        }
    }

    public bool IsServerPanelVisible => _role == "server";

    public bool IsClientPanelVisible => _role != "server";

    public string Port
    {
        get => _port;
        set => Set(ref _port, value);
    }

    public string Host
    {
        get => _host;
        set => Set(ref _host, value);
    }

    public string Zone
    {
        get => _zone;
        set => Set(ref _zone, value);
    }

    public string Password
    {
        get => _password;
        set => Set(ref _password, value);
    }

    public string ServerReceiveDir
    {
        get => _serverRecvDir;
        set
        {
            if (Set(ref _serverRecvDir, value))
            {
                RefreshReceivePathLabel();
            }
        }
    }

    public string ClientReceiveDir
    {
        get => _clientRecvDir;
        set
        {
            if (Set(ref _clientRecvDir, value))
            {
                RefreshReceivePathLabel();
            }
        }
    }

    public string StatusText
    {
        get => _statusText;
        private set => Set(ref _statusText, value);
    }

    public Color StatusColor
    {
        get => _statusColor;
        private set => Set(ref _statusColor, value);
    }

    public string PeerText
    {
        get => _peerText;
        private set => Set(ref _peerText, value);
    }

    public string HintText
    {
        get => _hintText;
        private set => Set(ref _hintText, value);
    }

    public string PendingSummary
    {
        get => _pendingSummary;
        private set => Set(ref _pendingSummary, value);
    }

    public string TaskText
    {
        get => _taskText;
        private set => Set(ref _taskText, value);
    }

    public string StatsText
    {
        get => _statsText;
        private set => Set(ref _statsText, value);
    }

    public double Progress
    {
        get => _progress;
        private set => Set(ref _progress, value);
    }

    public string ChatInput
    {
        get => _chatInput;
        set => Set(ref _chatInput, value);
    }

    public string ChatTitle
    {
        get => _chatTitle;
        private set => Set(ref _chatTitle, value);
    }

    public string ReceiveDirLabel
    {
        get => _receiveDirLabel;
        private set => Set(ref _receiveDirLabel, value);
    }

    public bool IsConnectTab
    {
        get => _isConnectTab;
        private set => Set(ref _isConnectTab, value);
    }

    public bool IsSendTab
    {
        get => _isSendTab;
        private set => Set(ref _isSendTab, value);
    }

    public bool IsReceiveTab
    {
        get => _isReceiveTab;
        private set => Set(ref _isReceiveTab, value);
    }

    public bool IsChatTab
    {
        get => _isChatTab;
        private set => Set(ref _isChatTab, value);
    }

    public PeerEntry? SelectedPeer
    {
        get => _selectedPeer;
        set
        {
            if (!Set(ref _selectedPeer, value))
            {
                return;
            }

            Raise(nameof(CanSetAlias));
            (SetAliasCommand as AsyncRelayCommand)?.RaiseCanExecuteChanged();
        }
    }

    public AddressItem? SelectedAddress
    {
        get => _selectedAddress;
        set
        {
            if (!Set(ref _selectedAddress, value))
            {
                return;
            }

            CanCopyAddress = _selectedAddress is not null
                             && _selectedAddress.Address.Contains(':')
                             && !_selectedAddress.Address.Contains("未检测");
            (CopyAddressCommand as AsyncRelayCommand)?.RaiseCanExecuteChanged();
        }
    }

    public PendingFile? SelectedPending
    {
        get => _selectedPending;
        set => Set(ref _selectedPending, value);
    }

    public TransferRow? SelectedRow
    {
        get => _selectedRow;
        set => Set(ref _selectedRow, value);
    }

    public bool ShowStorageWarning
    {
        get => _showStorageWarning;
        private set => Set(ref _showStorageWarning, value);
    }

    public string StorageWarningText
    {
        get => _storageWarningText;
        private set => Set(ref _storageWarningText, value);
    }

    public string ConfigButtonText => _role == "server" ? "分享配置" : "导入配置";

    public string SendButtonText => _role == "server" ? "开始发送" : "开始上传";

    public bool CanStartServer
    {
        get => _canStartServer;
        private set => Set(ref _canStartServer, value);
    }

    public bool CanStopServer
    {
        get => _canStopServer;
        private set => Set(ref _canStopServer, value);
    }

    public bool CanConnect
    {
        get => _canConnect;
        private set => Set(ref _canConnect, value);
    }

    public bool CanDisconnect
    {
        get => _canDisconnect;
        private set => Set(ref _canDisconnect, value);
    }

    public bool CanSend
    {
        get => _canSend;
        private set => Set(ref _canSend, value);
    }

    public bool CanCancel
    {
        get => _canCancel;
        private set => Set(ref _canCancel, value);
    }

    public bool CanChat
    {
        get => _canChat;
        private set => Set(ref _canChat, value);
    }

    public bool CanSetAlias
    {
        get => _canSetAlias;
        private set => Set(ref _canSetAlias, value);
    }

    public bool CanCopyAddress
    {
        get => _canCopyAddress;
        private set => Set(ref _canCopyAddress, value);
    }

    // ------------------------------------------------------------------ #
    // 命令
    // ------------------------------------------------------------------ #

    public ICommand SelectTabCommand { get; }
    public ICommand SelectRoleCommand { get; }
    public ICommand StartServerCommand { get; }
    public ICommand StopServerCommand { get; }
    public ICommand ConnectCommand { get; }
    public ICommand DisconnectCommand { get; }
    public ICommand AddFilesCommand { get; }
    public ICommand StartSendCommand { get; }
    public ICommand CancelCommand { get; }
    public ICommand RemovePendingCommand { get; }
    public ICommand ClearPendingCommand { get; }
    public ICommand BrowseServerDirCommand { get; }
    public ICommand BrowseClientDirCommand { get; }
    public ICommand CopyAddressCommand { get; }
    public ICommand RefreshAddressCommand { get; }
    public ICommand ConfigCommand { get; }
    public ICommand SetAliasCommand { get; }
    public ICommand CopyReceivePathCommand { get; }
    public ICommand RefreshReceiveListCommand { get; }
    public ICommand ShowRowDetailCommand { get; }
    public ICommand ShowFileDetailCommand { get; }
    public ICommand SendChatCommand { get; }
    public ICommand RequestStorageCommand { get; }

    // ------------------------------------------------------------------ #
    // 线程与事件泵
    // ------------------------------------------------------------------ #

    private static void Post(Action action)
    {
        if (MainThread.IsMainThread)
        {
            action();
        }
        else
        {
            MainThread.BeginInvokeOnMainThread(action);
        }
    }

    private static string NowStamp(long ts = 0)
    {
        var moment = ts > 0 ? DateTimeOffset.FromUnixTimeMilliseconds(ts).LocalDateTime : DateTime.Now;
        return moment.ToString("HH:mm:ss");
    }

    private void Log(string message) => AppendChat(message, fromPeer: false, system: true, quiet: true, ts: 0);

    // ------------------------------------------------------------------ #
    // 页签
    // ------------------------------------------------------------------ #

    private void SelectTab(string tag)
    {
        IsConnectTab = tag == "conn";
        IsSendTab = tag == "send";
        IsReceiveTab = tag == "recv";
        IsChatTab = tag == "chat";
        if (IsChatTab)
        {
            ChatTitle = "聊天";
        }
    }

    private void ShowTab(string tag) => SelectTab(tag);

    private void SelectRole(string role)
    {
        var target = role == "server" ? "server" : "client";
        if (_role == target)
        {
            return;
        }

        _ = ChangeRoleAsync(target);
    }

    // ------------------------------------------------------------------ #
    // 状态 / 按钮
    // ------------------------------------------------------------------ #

    private void SetState(string state, string? peer = null)
    {
        _state = state;
        if (peer is not null)
        {
            _peerAddress = peer;
            PeerText = peer;
        }

        StatusColor = state switch
        {
            StateListening => AppColors.Listen,
            StateConnected or StateFileOnly => AppColors.Online,
            StateBusy or StateConnecting => AppColors.Busy,
            _ => AppColors.Idle,
        };

        StatusText = state switch
        {
            StateConnecting => "连接中…",
            StateListening => "监听中",
            StateConnected => "已连接",
            StateFileOnly => "已连接（仅文件）",
            StateBusy => "传输中",
            _ => "未连接",
        };

        if (_role == "server" && state is StateListening or StateConnected)
        {
            StatusText = "监听中" + (_controls.Count > 0 ? $" · {_controls.Count} 个客户端在线" : "");
        }

        RefreshButtons();
    }

    private void RefreshButtons()
    {
        var busy = _state == StateBusy;
        var locked = _state is StateBusy or StateConnecting;
        var hasControl = _controls.Count > 0;
        var connected = hasControl || _state == StateFileOnly;
        var listening = _listener is not null;

        if (_role == "client")
        {
            CanConnect = !connected && !locked;
            CanDisconnect = connected;
            CanSend = connected && !locked && PendingFiles.Count > 0;
            CanStartServer = false;
            CanStopServer = false;
        }
        else
        {
            CanStartServer = !listening && !locked;
            CanStopServer = listening && !locked;
            CanSend = listening && !locked && hasControl && PendingFiles.Count > 0;
            CanConnect = false;
            CanDisconnect = false;
        }

        CanCancel = busy;
        CanChat = hasControl;
        CanSetAlias = _role == "server" && Peers.Count > 0;
        UpdateHint();

        (StartServerCommand as RelayCommand)?.RaiseCanExecuteChanged();
        (StopServerCommand as RelayCommand)?.RaiseCanExecuteChanged();
        (ConnectCommand as RelayCommand)?.RaiseCanExecuteChanged();
        (DisconnectCommand as RelayCommand)?.RaiseCanExecuteChanged();
        (StartSendCommand as RelayCommand)?.RaiseCanExecuteChanged();
        (CancelCommand as RelayCommand)?.RaiseCanExecuteChanged();
        (SendChatCommand as AsyncRelayCommand)?.RaiseCanExecuteChanged();
        (SetAliasCommand as AsyncRelayCommand)?.RaiseCanExecuteChanged();
    }

    private void UpdateHint()
    {
        if (_role == "server")
        {
            HintText = _controls.Count > 0
                ? $"{_controls.Count} 个客户端在线：不选中则群发，选中则只发给选中项。"
                : "还没有客户端连接；客户端连接后即可收发消息。";
            return;
        }

        if (_controls.Count > 0)
        {
            HintText = $"已连接 {_peerAddress}，点「发送」发出消息。";
        }
        else if (_fileOnlyMode)
        {
            HintText = "仅文件模式：对方是命令行服务器，不支持文字消息。";
        }
        else
        {
            HintText = "未连接：先在「连接」页填服务器地址并连接。";
        }
    }

    // ------------------------------------------------------------------ #
    // 角色
    // ------------------------------------------------------------------ #

    private async Task ChangeRoleAsync(string role)
    {
        if (_role == role)
        {
            return;
        }

        if (_state == StateBusy)
        {
            await _dialogs.AlertAsync("正在传输", "请等传输结束或取消后再切换角色。");
            Raise(nameof(IsServerRole), nameof(IsClientRole));
            return;
        }

        StopServer(silent: true);
        DisconnectPeer(silent: true);
        Role = role;
        _recvSignature = "";
        RefreshPeersView();
        RefreshLocalList(true);
        RefreshButtons();
        Log(role == "server" ? "已切换到服务器模式。" : "已切换到客户端模式。");
    }

    // ------------------------------------------------------------------ #
    // 控制通道登记
    // ------------------------------------------------------------------ #

    private void AdoptControl(ControlLink? link, string key, string peer = "")
    {
        if (link is null)
        {
            if (!string.IsNullOrEmpty(key))
            {
                if (_controls.TryGetValue(key, out var existing) && !existing.IsClosed)
                {
                    return;
                }

                if (_controls.Remove(key))
                {
                    _controlPeers.Remove(key);
                    Log("连接已断开。");
                }
            }
            else
            {
                foreach (var old in _controls.Values.ToList())
                {
                    try
                    {
                        old.Close();
                    }
                    catch (Exception)
                    {
                        // 忽略
                    }
                }

                _controls.Clear();
                _controlPeers.Clear();
            }
        }
        else
        {
            if (_controls.TryGetValue(key, out var old) && !ReferenceEquals(old, link))
            {
                try
                {
                    old.Close();
                }
                catch (Exception)
                {
                    // 忽略
                }
            }

            _controls[key] = link;
            _controlPeers[key] = string.IsNullOrEmpty(peer) ? link.Peer : peer;

            if (_role == "client")
            {
                _peerAddress = string.IsNullOrEmpty(peer) ? link.Peer : peer;
                PeerText = _peerAddress;
                Log($"已连接到 {_peerAddress}。");
                ShowTab("send");
            }
            else
            {
                Log($"客户端 {PeerLabel(key)} 已连接。");
            }
        }

        if (_role == "server")
        {
            RefreshPeersView();
            SetState(_controls.Count > 0
                ? StateConnected
                : (_listener is not null ? StateListening : StateIdle));
        }
        else
        {
            if (_controls.Count > 0)
            {
                SetState(StateConnected);
            }
            else
            {
                _peerAddress = "";
                PeerText = "";
                if (_state is StateConnected or StateFileOnly)
                {
                    SetState(_listener is not null ? StateListening : StateIdle);
                }
            }
        }

        RefreshButtons();
    }

    private void RefreshPeersView()
    {
        if (_role != "server")
        {
            return;
        }

        var selectedKeys = Peers.Where(item => ReferenceEquals(item, _selectedPeer))
            .Select(item => item.Key)
            .ToHashSet(StringComparer.Ordinal);

        var keys = _controls.Keys.OrderBy(item => item, StringComparer.Ordinal).ToList();
        _peerKeys.Clear();
        _peerKeys.AddRange(keys);

        Peers.Clear();
        foreach (var key in keys)
        {
            Peers.Add(new PeerEntry(key, _controlPeers.GetValueOrDefault(key, key), PeerLabel(key)));
        }

        _selectedPeer = Peers.FirstOrDefault(item => selectedKeys.Contains(item.Key));
        Raise(nameof(SelectedPeer));

        PeerText = keys.Count > 0 ? $"{keys.Count} 个客户端在线" : "";
        CanSetAlias = keys.Count > 0;
        (SetAliasCommand as AsyncRelayCommand)?.RaiseCanExecuteChanged();
    }

    private string AliasForPeer(string peer)
    {
        if (_role != "server")
        {
            return "";
        }

        return _clientAliases.GetValueOrDefault(PeerFormatter.IpOf(peer), "");
    }

    private string PeerLabel(string key, bool withAddress = true)
    {
        var peer = _controlPeers.GetValueOrDefault(key, key);
        var alias = AliasForPeer(peer);
        if (!string.IsNullOrEmpty(alias))
        {
            return withAddress ? $"{alias}  ({peer})" : alias;
        }

        return peer;
    }

    private string RemoteLabel(string peer, string? clientId = null)
    {
        if (_role == "client")
        {
            return "服务器";
        }

        if (!string.IsNullOrEmpty(clientId) && _controlPeers.TryGetValue(clientId!, out var mapped))
        {
            peer = mapped;
        }

        var address = PeerFormatter.IpOf(peer);
        var alias = _clientAliases.GetValueOrDefault(address, "");
        return !string.IsNullOrEmpty(alias) ? alias : (!string.IsNullOrEmpty(address) ? address : "—");
    }

    private List<(string Key, ControlLink Link)> TargetPeerLinks()
    {
        if (_controls.Count == 0)
        {
            return new List<(string, ControlLink)>();
        }

        if (_role == "client")
        {
            return _controls.Select(pair => (pair.Key, pair.Value)).ToList();
        }

        if (_selectedPeer is null)
        {
            return _controls.Select(pair => (pair.Key, pair.Value)).ToList();
        }

        var selected = _selectedPeer!.Key;
        return _controls.TryGetValue(selected, out var link)
            ? new List<(string, ControlLink)> { (selected, link) }
            : new List<(string, ControlLink)>();
    }

    // ------------------------------------------------------------------ #
    // 服务器
    // ------------------------------------------------------------------ #

    private void StartServer()
    {
        if (_listener is not null)
        {
            return;
        }

        if (!int.TryParse(Port?.Trim(), out var port) || port is < 1 or > 65535)
        {
            _ = _dialogs.AlertAsync("端口无效", "端口必须是 1..65535 的整数");
            return;
        }

        var directory = string.IsNullOrWhiteSpace(ServerReceiveDir)
            ? MobileStorage.DefaultDirectory("received")
            : ServerReceiveDir.Trim();
        if (!MobileStorage.TryEnsureDirectory(directory, out var error))
        {
            _ = _dialogs.AlertAsync("接收目录不可用", error);
            return;
        }

        ServerReceiveDir = directory;
        if ((Password ?? "").Length > ProtocolConst.MaxPasswordChars)
        {
            _ = _dialogs.AlertAsync("密码太长", $"连接密码最多 {ProtocolConst.MaxPasswordChars} 个字符");
            return;
        }

        Socket socket;
        try
        {
            socket = Handshake.MakeListener("::", port, false);
        }
        catch (Exception ex)
        {
            _ = _dialogs.AlertAsync("监听失败", $"无法监听 [::]:{port}\n{ex.Message}");
            return;
        }

        _listener = socket;
        _acceptCts = new CancellationTokenSource();
        _pushRegistry.Clear();
        _recvSignature = "";
        var token = _acceptCts.Token;
        _ = Task.Run(() => AcceptLoopAsync(socket, token));

        SetState(StateListening);
        RefreshAddresses();
        ShowTab("send");
        Log($"服务器已启动，端口 {port}，接收目录：{directory}");
        if (!string.IsNullOrEmpty(Password))
        {
            Log("已启用连接密码，客户端必须填写相同密码才能连接。");
        }
    }

    private void StopServer(bool silent = false)
    {
        if (_listener is null)
        {
            return;
        }

        _acceptCts?.Cancel();
        try
        {
            _listener.Close();
        }
        catch (Exception)
        {
            // 忽略
        }

        _listener = null;
        _acceptCts?.Dispose();
        _acceptCts = null;
        _pushRegistry.Clear();

        foreach (var link in _controls.Values.ToList())
        {
            try
            {
                link.Close();
            }
            catch (Exception)
            {
                // 忽略
            }
        }

        _controls.Clear();
        _controlPeers.Clear();
        RefreshPeersView();
        SetState(StateIdle);
        if (!silent)
        {
            Log("服务器已停止。");
        }
    }

    private async Task AcceptLoopAsync(Socket listener, CancellationToken token)
    {
        while (!token.IsCancellationRequested)
        {
            Socket client;
            try
            {
                client = await listener.AcceptAsync(token).ConfigureAwait(false);
            }
            catch (Exception)
            {
                break;
            }

            _ = Task.Run(() => ServeClient(client));
        }
    }

    private void ServeClient(Socket client)
    {
        var peer = PeerFormatter.Format(client.RemoteEndPoint);
        var conn = new FramedConnection(client);
        var keepSocket = false;
        try
        {
            var hello = Handshake.ReadHello(conn);
            var clientId = hello.GetStr("client_id") ?? peer;
            Handshake.Authenticate(conn, ServerReceiveDir, Password ?? "");

            if (hello.GetStr("role") == "control")
            {
                var link = ControlLink.FromAccepted(
                    conn,
                    peer,
                    onText: (text, ts) => Post(() => OnPeerText(text, ts, peer)),
                    onState: up =>
                    {
                        if (!up)
                        {
                            Post(() => AdoptControl(null, clientId, peer));
                        }
                    },
                    onPush: (name, size, token) => Post(() => OnPeerPush(name, size, token, peer)),
                    onNotice: (text, ts) => Post(() => OnPeerNotice(text, ts, peer)));
                Post(() => AdoptControl(link, clientId, peer));
                keepSocket = true;
                return;
            }

            ServerSession.HandleTransferSession(
                conn,
                ServerReceiveDir,
                timeoutSeconds: 120,
                locks: _nameLocks,
                emit: e => Post(() => OnServerEvent(peer, clientId, e)),
                pathResolver: ResolvePushPath);
        }
        catch (HelloRejectedException)
        {
            // 协议层已经回送过错误
        }
        catch (ProtocolException ex)
        {
            Post(() => Log($"{peer} 协议错误：{ex.Message}"));
        }
        catch (Exception ex) when (ErrorKinds.IsTransportError(ex))
        {
            // 对端断开，属正常
        }
        catch (Exception ex)
        {
            Post(() => Log($"{peer} 会话异常：{ex.Message}"));
        }
        finally
        {
            if (!keepSocket)
            {
                conn.Dispose();
            }

            Post(() => OnServerSessionEnd(peer));
        }
    }

    private string? ResolvePushPath(string token)
    {
        if (_pushRegistry.TryGetValue(token ?? "", out var path) && File.Exists(path))
        {
            return path;
        }

        return null;
    }

    // ------------------------------------------------------------------ #
    // 客户端连接
    // ------------------------------------------------------------------ #

    private void ConnectPeer()
    {
        if (_controls.Count > 0)
        {
            return;
        }

        var host = (Host ?? "").Trim();
        if (host.Length == 0)
        {
            _ = _dialogs.AlertAsync("地址为空", "请填写服务器的 IPv6 地址");
            return;
        }

        if (!int.TryParse((Port ?? "").Trim(), out var port) || port is < 1 or > 65535)
        {
            _ = _dialogs.AlertAsync("端口无效", "端口必须是 1..65535 的整数");
            return;
        }

        if ((Password ?? "").Length > ProtocolConst.MaxPasswordChars)
        {
            _ = _dialogs.AlertAsync("密码太长", $"连接密码最多 {ProtocolConst.MaxPasswordChars} 个字符");
            return;
        }

        var zone = (Zone ?? "").Trim();
        var password = Password ?? "";
        SetState(StateConnecting);
        _ = Task.Run(() => ConnectWorker(host, port, zone, password));
    }

    private void ConnectWorker(string host, int port, string zone, string password)
    {
        try
        {
            var link = ControlLink.Connect(
                host,
                port,
                zone,
                timeoutSeconds: 120,
                connectTimeoutSeconds: 10,
                onText: (text, ts) => Post(() => OnPeerText(text, ts, "服务器")),
                onState: up =>
                {
                    if (!up)
                    {
                        Post(() => AdoptControl(null, ClientId, ""));
                    }
                },
                onPush: (name, size, token) => Post(() => OnPeerPush(name, size, token, "服务器")),
                onNotice: (text, ts) => Post(() => OnPeerNotice(text, ts, "服务器")),
                clientId: ClientId,
                password: password);
            Post(() => AdoptControl(link, ClientId, link.Peer));
        }
        catch (ConnectException ex)
        {
            var message = ex.Message;
            if (message.Contains("未启用文本消息", StringComparison.Ordinal))
            {
                Post(() =>
                {
                    _fileOnlyMode = true;
                    Log("对端不支持文字消息，已进入仅文件模式。");
                    SetState(StateFileOnly, $"[{host}]:{port}");
                });
            }
            else
            {
                Post(() =>
                {
                    Log($"连接失败：{message}");
                    SetState(StateIdle);
                    _ = _dialogs.AlertAsync("连接失败", message);
                });
            }
        }
    }

    private void DisconnectPeer(bool silent = false)
    {
        foreach (var link in _controls.Values.ToList())
        {
            try
            {
                link.Close();
            }
            catch (Exception)
            {
                // 忽略
            }
        }

        _controls.Clear();
        _controlPeers.Clear();
        _fileOnlyMode = false;
        _peerAddress = "";
        PeerText = "";
        SetState(StateIdle);
        RefreshButtons();
        if (!silent)
        {
            Log("已断开连接。");
        }
    }

    // ------------------------------------------------------------------ #
    // 待发送文件
    // ------------------------------------------------------------------ #

    private async Task AddFilesAsync()
    {
        var start = _pendingLastDir;
        var path = await _dialogs.PickPathAsync(false, start);
        if (string.IsNullOrEmpty(path))
        {
            return;
        }

        _pendingLastDir = Path.GetDirectoryName(path) ?? start;
        AddPendingFiles(new[] { path! });
    }

    private string _pendingLastDir = MobileStorage.DisplayRoot;

    private void AddPendingFiles(IEnumerable<string> paths)
    {
        var known = new HashSet<string>(PendingFiles.Select(item => SafeFullPath(item.Path)), StringComparer.Ordinal);
        var added = 0;
        foreach (var raw in paths)
        {
            if (string.IsNullOrWhiteSpace(raw) || !File.Exists(raw))
            {
                continue;
            }

            var full = SafeFullPath(raw);
            if (!known.Add(full))
            {
                continue;
            }

            long size;
            try
            {
                size = new FileInfo(full).Length;
            }
            catch (Exception)
            {
                size = 0;
            }

            PendingFiles.Add(new PendingFile(full, size));
            added++;
        }

        if (added > 0)
        {
            RefreshPendingView();
            Log($"已添加 {added} 个待发送文件。");
        }
    }

    private static string SafeFullPath(string path)
    {
        try
        {
            return Path.GetFullPath(path);
        }
        catch (Exception)
        {
            return path;
        }
    }

    private void RefreshPendingView()
    {
        var total = PendingFiles.Sum(item => item.Size);
        PendingSummary = PendingFiles.Count > 0
            ? $"待发送 {PendingFiles.Count} 个，共 {Formats.HumanBytes(total)}"
            : "还没有选择文件";
        Raise(nameof(PendingSummary));
        RefreshButtons();
    }

    private void RemoveSelectedPending()
    {
        if (_selectedPending is null)
        {
            return;
        }

        PendingFiles.Remove(_selectedPending);
        SelectedPending = null;
        RefreshPendingView();
    }

    private void ClearPending()
    {
        PendingFiles.Clear();
        SelectedPending = null;
        RefreshPendingView();
    }

    private async Task BrowseServerDirAsync()
    {
        var path = await _dialogs.PickPathAsync(true, ServerReceiveDir);
        if (!string.IsNullOrEmpty(path))
        {
            ServerReceiveDir = path!;
            _recvSignature = "";
            RefreshLocalList(true);
        }
    }

    private async Task BrowseClientDirAsync()
    {
        var path = await _dialogs.PickPathAsync(true, ClientReceiveDir);
        if (!string.IsNullOrEmpty(path))
        {
            ClientReceiveDir = path!;
            _recvSignature = "";
            RefreshLocalList(true);
        }
    }

    // ------------------------------------------------------------------ #
    // 发送
    // ------------------------------------------------------------------ #

    private void StartSend()
    {
        if (_role == "client")
        {
            StartUpload();
        }
        else
        {
            SendFromServer();
        }
    }

    private void StartUpload()
    {
        if (_state is StateIdle or StateListening or StateBusy or StateConnecting)
        {
            return;
        }

        var files = PendingFiles.Where(item => File.Exists(item.Path)).ToList();
        if (files.Count == 0)
        {
            return;
        }

        var pairs = new List<(string Path, string RowId)>();
        foreach (var file in files)
        {
            pairs.Add((file.Path, NewRow("上传", file.Name, file.Size)));
        }

        PendingFiles.Clear();
        SelectedPending = null;
        RefreshPendingView();

        _transferCts = new CancellationTokenSource();
        _transferSocket = null;
        SetState(StateBusy);
        Log($"开始上传 {pairs.Count} 个文件。");

        var host = (Host ?? "").Trim();
        var port = int.TryParse((Port ?? "").Trim(), out var parsed) ? parsed : 9000;
        var zone = (Zone ?? "").Trim();
        var password = Password ?? "";
        _ = Task.Run(() => UploadWorker(pairs, host, port, zone, password));
    }

    private void UploadWorker(List<(string Path, string RowId)> pairs, string host, int port, string zone, string password)
    {
        var cancel = _transferCts!;
        FramedConnection? conn = null;
        try
        {
            var peer = Handshake.ResolveAndConnect(
                host, port, zone, 10, 120, "transfer", "file_gui_mobile/1.0",
                new Dictionary<string, object?> { ["client_id"] = ClientId }, password);
            conn = peer.Conn;
            _transferSocket = conn.Socket;

            foreach (var (path, rowId) in pairs)
            {
                if (cancel.IsCancellationRequested)
                {
                    Post(() => HandleCancelled(rowId));
                    continue;
                }

                Post(() => HandleStart(rowId));
                var id = rowId;
                var result = FileTransfer.SendFile(
                    conn,
                    path,
                    resume: true,
                    onProgress: snapshot => Post(() => HandleProgress(id, snapshot)),
                    onStage: stage => Post(() => HandleStage(id, stage)),
                    shouldCancel: () => cancel.IsCancellationRequested);

                if (cancel.IsCancellationRequested && !result.Ok)
                {
                    Post(() => HandleCancelled(id));
                }
                else if (result.Ok)
                {
                    Post(() => HandleDone(id, result));
                }
                else
                {
                    Post(() => HandleFailed(id, result.Error, result));
                }
            }
        }
        catch (Exception ex) when (ex is ConnectException or IOException or SocketException or ProtocolException)
        {
            if (cancel.IsCancellationRequested)
            {
                foreach (var (_, rowId) in pairs)
                {
                    var id = rowId;
                    if (!_rows.TryGetValue(id, out var row) || !row.Finished)
                    {
                        Post(() => HandleCancelled(id));
                    }
                }
            }
            else
            {
                Post(() => Log($"上传中断：{ex.Message}"));
                foreach (var (_, rowId) in pairs)
                {
                    var id = rowId;
                    if (!_rows.TryGetValue(id, out var row) || !row.Finished)
                    {
                        Post(() => HandleFailed(id, ex.Message, null));
                    }
                }
            }
        }
        finally
        {
            if (conn is not null)
            {
                try
                {
                    conn.SendMsg(new Dictionary<string, object?> { ["cmd"] = "bye" });
                    conn.RecvMsg();
                }
                catch (Exception)
                {
                    // 对端可能已经走了
                }

                conn.Dispose();
            }

            Post(FinishBatch);
        }
    }

    private void SendFromServer()
    {
        var files = PendingFiles.Where(item => File.Exists(item.Path)).ToList();
        if (files.Count == 0)
        {
            _ = _dialogs.AlertAsync("没有文件", "请先添加要发送的文件。");
            return;
        }

        var targets = TargetPeerLinks();
        if (targets.Count == 0)
        {
            _ = _dialogs.AlertAsync("没有客户端", "还没有客户端连接，无法发送。");
            return;
        }

        var sent = 0;
        var failed = 0;
        foreach (var file in files)
        {
            foreach (var (key, link) in targets)
            {
                var token = Guid.NewGuid().ToString("N");
                _pushRegistry[token] = file.Path;
                try
                {
                    link.SendPush(file.Name, file.Size, token);
                    sent++;
                }
                catch (Exception ex) when (ex is IOException or ProtocolException or ArgumentException or SocketException)
                {
                    failed++;
                    _pushRegistry.Remove(token);
                    Log($"发送给 {PeerLabel(key)} 失败：{ex.Message}");
                }
            }
        }

        PendingFiles.Clear();
        SelectedPending = null;
        RefreshPendingView();
        Log($"已通知 {targets.Count} 个客户端接收 {files.Count} 个文件"
            + (failed > 0 ? $"（失败 {failed} 次）" : "。"));
    }

    // ------------------------------------------------------------------ #
    // 接收 / 推送
    // ------------------------------------------------------------------ #

    private void OnPeerPush(string name, long size, string? token, string peer)
    {
        if (_role != "client")
        {
            return;
        }

        if (_state is not (StateBusy or StateConnecting) && _controls.Count > 0)
        {
            _pushQueue.Add((name, token));
            Log($"收到服务器发来的文件：{name}（{Formats.HumanBytes(size)}）");
            var items = _pushQueue.ToList();
            _pushQueue.Clear();
            StartReceive(items);
            return;
        }

        _pushQueue.Add((name, token));
        Log($"收到服务器发来的文件：{name}（{Formats.HumanBytes(size)}），排队等待接收。");
    }

    private void StartReceive(List<(string Name, string? Token)> items)
    {
        if (_state is StateBusy or StateConnecting || items.Count == 0)
        {
            return;
        }

        var target = CurrentReceiveDir();
        if (!MobileStorage.TryEnsureDirectory(target, out var error))
        {
            _ = _dialogs.AlertAsync("接收目录不可用", error);
            return;
        }

        var pairs = items.Select(item => (item.Name, item.Token, NewRow("推送", item.Name, 0))).ToList();
        _transferCts = new CancellationTokenSource();
        _transferSocket = null;
        SetState(StateBusy);
        Log($"开始接收 {pairs.Count} 个文件到 {target}。");

        var host = (Host ?? "").Trim();
        var port = int.TryParse((Port ?? "").Trim(), out var parsed) ? parsed : 9000;
        var zone = (Zone ?? "").Trim();
        var password = Password ?? "";
        _ = Task.Run(() => DownloadWorker(pairs, target, host, port, zone, password));
    }

    private void DownloadWorker(
        List<(string Name, string? Token, string RowId)> pairs,
        string target,
        string host,
        int port,
        string zone,
        string password)
    {
        var cancel = _transferCts!;
        FramedConnection? conn = null;
        try
        {
            var peer = Handshake.ResolveAndConnect(
                host, port, zone, 10, 120, "transfer", "file_gui_mobile/1.0",
                new Dictionary<string, object?> { ["client_id"] = ClientId }, password);
            conn = peer.Conn;
            _transferSocket = conn.Socket;

            foreach (var (name, token, rowId) in pairs)
            {
                if (cancel.IsCancellationRequested)
                {
                    Post(() => HandleCancelled(rowId));
                    continue;
                }

                Post(() => HandleStart(rowId));
                var id = rowId;
                var result = FileTransfer.RecvFile(
                    conn,
                    name,
                    target,
                    resume: true,
                    onProgress: snapshot => Post(() => HandleProgress(id, snapshot)),
                    onStage: stage => Post(() => HandleStage(id, stage)),
                    shouldCancel: () => cancel.IsCancellationRequested,
                    token: token);

                if (cancel.IsCancellationRequested && !result.Ok)
                {
                    Post(() => HandleCancelled(id));
                }
                else if (result.Ok)
                {
                    Post(() => HandleDone(id, result));
                }
                else
                {
                    Post(() => HandleFailed(id, result.Error, result));
                }
            }
        }
        catch (Exception ex) when (ex is ConnectException or IOException or SocketException or ProtocolException)
        {
            if (cancel.IsCancellationRequested)
            {
                foreach (var (_, _, rowId) in pairs)
                {
                    var id = rowId;
                    if (!_rows.TryGetValue(id, out var row) || !row.Finished)
                    {
                        Post(() => HandleCancelled(id));
                    }
                }
            }
            else
            {
                Post(() => Log($"接收中断：{ex.Message}"));
                foreach (var (_, _, rowId) in pairs)
                {
                    var id = rowId;
                    if (!_rows.TryGetValue(id, out var row) || !row.Finished)
                    {
                        Post(() => HandleFailed(id, ex.Message, null));
                    }
                }
            }
        }
        finally
        {
            if (conn is not null)
            {
                try
                {
                    conn.SendMsg(new Dictionary<string, object?> { ["cmd"] = "bye" });
                    conn.RecvMsg();
                }
                catch (Exception)
                {
                    // 对端可能已经走了
                }

                conn.Dispose();
            }

            Post(FinishBatch);
        }
    }

    private void CancelTransfer()
    {
        if (_transferCts is null)
        {
            return;
        }

        try
        {
            _transferCts.Cancel();
        }
        catch (Exception)
        {
            // 忽略
        }

        var socket = _transferSocket;
        if (socket is not null)
        {
            try
            {
                socket.Close();
            }
            catch (Exception)
            {
                // 忽略
            }
        }

        Log("正在取消传输（未完成部分保留为 .part，可续传）。");
    }

    private void FinishBatch()
    {
        _transferCts?.Dispose();
        _transferCts = null;
        _transferSocket = null;
        Progress = 0;
        TaskText = "空闲";
        StatsText = "等待任务";

        if (_controls.Count > 0)
        {
            SetState(StateConnected);
        }
        else if (_listener is not null)
        {
            SetState(StateListening);
        }
        else if (_fileOnlyMode)
        {
            SetState(StateFileOnly);
        }
        else
        {
            SetState(StateIdle);
        }

        RefreshLocalList(true);
        RefreshButtons();

        if (_role == "client" && _pushQueue.Count > 0 && _controls.Count > 0)
        {
            var items = _pushQueue.ToList();
            _pushQueue.Clear();
            StartReceive(items);
        }
    }

    private string CurrentReceiveDir()
    {
        var raw = _role == "client" ? ClientReceiveDir : ServerReceiveDir;
        var value = string.IsNullOrWhiteSpace(raw)
            ? MobileStorage.DefaultDirectory(_role == "client" ? "downloads" : "received")
            : raw.Trim();
        try
        {
            return Path.GetFullPath(value);
        }
        catch (Exception)
        {
            return value;
        }
    }

    private void RefreshReceivePathLabel()
    {
        ReceiveDirLabel = $"接收目录：{CurrentReceiveDir()}";
    }

    private void RefreshLocalList(bool force = false)
    {
        RefreshReceivePathLabel();
        var directory = CurrentReceiveDir();
        var files = new List<(string Name, long Size, DateTime Mtime)>();
        try
        {
            if (Directory.Exists(directory))
            {
                foreach (var path in Directory.EnumerateFiles(directory))
                {
                    var name = Path.GetFileName(path);
                    if (name.EndsWith(".part", StringComparison.Ordinal))
                    {
                        continue;
                    }

                    try
                    {
                        var info = new FileInfo(path);
                        files.Add((name, info.Length, info.LastWriteTime));
                    }
                    catch (Exception)
                    {
                        // 单个文件读不到就跳过
                    }
                }
            }
        }
        catch (Exception)
        {
            files.Clear();
        }

        files.Sort((a, b) => string.Compare(a.Name, b.Name, StringComparison.OrdinalIgnoreCase));
        var entries = files
            .Take(MaxUiFiles)
            .Select(item => new FileEntry
            {
                Name = item.Name,
                SizeText = Formats.HumanBytes(item.Size),
                MtimeText = item.Mtime.ToString("MM-dd HH:mm"),
                FullPath = Path.Combine(directory, item.Name),
            })
            .ToList();

        var signature = string.Join("|", entries.Select(item => $"{item.Name}:{item.SizeText}:{item.MtimeText}"));
        if (!force && signature == _recvSignature)
        {
            return;
        }

        _recvSignature = signature;
        ReceivedFiles.Clear();
        foreach (var entry in entries)
        {
            ReceivedFiles.Add(entry);
        }
    }

    private async Task CopyReceivePathAsync()
    {
        var directory = CurrentReceiveDir();
        MobileStorage.TryEnsureDirectory(directory, out _);
        if (await _dialogs.CopyAsync(directory))
        {
            Log($"已复制接收目录：{directory}");
        }
    }

    private void StartAutoRefresh()
    {
        _autoRefreshCts = new CancellationTokenSource();
        var token = _autoRefreshCts.Token;
        _ = Task.Run(async () =>
        {
            var timer = new PeriodicTimer(TimeSpan.FromSeconds(AutoRefreshSeconds));
            try
            {
                while (await timer.WaitForNextTickAsync(token).ConfigureAwait(false))
                {
                    Post(() => RefreshLocalList());
                }
            }
            catch (Exception)
            {
                // 取消 / 退出
            }
            finally
            {
                timer.Dispose();
            }
        });
    }

    // ------------------------------------------------------------------ #
    // 传输记录
    // ------------------------------------------------------------------ #

    private string NewRow(string direction, string name, long size)
    {
        _rowSequence++;
        var id = $"row{_rowSequence}";
        var remote = _role == "client" ? "服务器" : "—";
        var row = new TransferRow
        {
            Id = id,
            Name = name,
            Direction = direction,
            Remote = remote,
            Status = "等待",
            Check = "—",
            Size = size,
            Matched = null,
        };
        _rows[id] = row;
        History.Add(row);
        RefreshButtons();
        return id;
    }

    private void OnServerEvent(string peer, string clientId, TransferEventArgs e)
    {
        switch (e.Type)
        {
            case "start":
            {
                var direction = e.Direction == "receive" ? "接收" : "发送";
                var rowId = NewRow(direction, e.Name, e.Size);
                var row = _rows[rowId];
                row.ServerPeer = peer;
                row.ServerClientId = clientId;
                row.Remote = RemoteLabel(peer, clientId);
                row.Status = "进行中";
                _serverRowIds[ServerKey(peer, e.Direction, e.Name)] = rowId;
                TaskText = $"{direction}：{e.Name}";
                Progress = 0;
                StatsText = "正在传输…";
                break;
            }

            case "progress":
            {
                if (_serverRowIds.TryGetValue(ServerKey(peer, e.Direction, e.Name), out var rowId))
                {
                    HandleProgress(rowId, new ProgressSnapshot(e.Done, e.Total, e.Fraction, e.Rate, e.Eta, 0));
                }

                break;
            }

            case "done":
            {
                if (e.Result is not null
                    && _serverRowIds.TryGetValue(ServerKey(peer, e.Result.Direction, e.Result.Name), out var rowId))
                {
                    HandleDone(rowId, e.Result);
                }

                break;
            }

            case "error":
            {
                if (_serverRowIds.TryGetValue(ServerKey(peer, e.Direction, e.Name), out var rowId))
                {
                    HandleFailed(rowId, e.Error, null);
                }
                else
                {
                    Log($"{peer} {e.Name}：{e.Error}");
                }

                break;
            }
        }
    }

    private static string ServerKey(string peer, string direction, string name)
        => $"{peer}|{direction}|{name}";

    private void OnServerSessionEnd(string peer)
    {
        foreach (var pair in _serverRowIds.ToList())
        {
            if (!pair.Key.StartsWith(peer + "|", StringComparison.Ordinal))
            {
                continue;
            }

            if (_rows.TryGetValue(pair.Value, out var row) && !row.Finished)
            {
                HandleFailed(pair.Value, "连接中断，未完成部分已保留为 .part", null);
            }
        }
    }

    private void HandleStart(string rowId)
    {
        if (!_rows.TryGetValue(rowId, out var row))
        {
            return;
        }

        row.Status = "进行中";
        TaskText = $"{row.Direction}：{row.Name}";
        Progress = 0;
        StatsText = "正在传输…";
    }

    private void HandleProgress(string rowId, ProgressSnapshot snapshot)
    {
        if (!_rows.TryGetValue(rowId, out var row))
        {
            return;
        }

        if (snapshot.Total > 0)
        {
            row.Size = snapshot.Total;
        }

        Progress = snapshot.Fraction;
        var etaText = snapshot.Eta is > 0 ? Formats.FormatDuration(snapshot.Eta) : "--:--";
        StatsText = $"{(int)Math.Round(snapshot.Fraction * 100)}% · {Formats.HumanBytes(snapshot.Done)}/" +
                    $"{Formats.HumanBytes(snapshot.Total)} · {Formats.HumanBytes(snapshot.Rate)}/s · 剩余 {etaText}";
        TaskText = $"{row.Direction}：{row.Name}";
    }

    private void HandleStage(string rowId, TransferStageInfo stage)
    {
        if (!_rows.TryGetValue(rowId, out var row))
        {
            return;
        }

        if (stage.Stage == "restart")
        {
            Log($"{row.Name} 校验不一致，正在从头重传。");
        }
        else if (stage.Stage == "start")
        {
            row.ResumedFrom = stage.ResumedFrom;
            if (stage.ResumedFrom > 0)
            {
                Log($"{row.Name} 从 {Formats.HumanBytes(stage.ResumedFrom)} 处续传。");
            }
        }
    }

    private void HandleDone(string rowId, TransferResult result)
    {
        if (!_rows.TryGetValue(rowId, out var row))
        {
            return;
        }

        row.Name = string.IsNullOrEmpty(result.FinalName) ? row.Name : result.FinalName;
        row.Size = result.Size;
        row.Status = "完成";
        row.LocalSha256 = result.LocalSha256;
        row.PeerSha256 = result.PeerSha256;
        row.Matched = result.Matched;
        row.ResumedFrom = result.ResumedFrom;
        row.Check = result.Matched ? "一致" : "不一致";
        row.Finished = true;

        Progress = 1;
        StatsText = $"100% · {Formats.HumanBytes(row.Size)}";
        Log($"{row.Direction}完成：{row.Name}，{(result.Matched ? "哈希一致" : "哈希不一致")}");

        if (row.Direction == "接收")
        {
            var verdict = result.Matched ? "SHA-256 校验通过" : "哈希不一致";
            SendNotice(row.ServerClientId, $"已收到文件 {row.Name}（{Formats.HumanBytes(row.Size)}，{verdict}）");
            MaybeFinishServer();
        }
        else if (row.Direction == "发送")
        {
            MaybeFinishServer();
        }
        else if (row.Direction == "推送" && result.Matched)
        {
            SendNotice("", $"手机已接收文件 {row.Name}（校验一致）");
        }
    }

    private void HandleFailed(string rowId, string message, TransferResult? result)
    {
        if (!_rows.TryGetValue(rowId, out var row))
        {
            return;
        }

        row.Status = "失败";
        row.Finished = true;
        if (!string.IsNullOrEmpty(result?.LocalSha256))
        {
            row.LocalSha256 = result!.LocalSha256;
        }

        if (!string.IsNullOrEmpty(result?.PeerSha256))
        {
            row.PeerSha256 = result!.PeerSha256;
        }

        Log($"{row.Direction}失败：{row.Name} —— {message}");

        if (row.Direction == "接收")
        {
            SendNotice(row.ServerClientId, $"文件 {row.Name} 接收失败：{message}");
            MaybeFinishServer();
        }
    }

    private void HandleCancelled(string rowId)
    {
        if (!_rows.TryGetValue(rowId, out var row))
        {
            return;
        }

        row.Status = "已取消";
        row.Finished = true;
        Log($"{row.Direction}已取消：{row.Name}（.part 已保留）");
    }

    private void MaybeFinishServer()
    {
        var pending = _rows.Values.Any(row => row.Direction is "接收" or "发送" && !row.Finished);
        if (pending)
        {
            return;
        }

        Progress = 0;
        TaskText = "空闲";
        StatsText = "等待任务";
        RefreshLocalList(true);
        RefreshButtons();
    }

    // ------------------------------------------------------------------ #
    // 记录详情
    // ------------------------------------------------------------------ #

    public async Task ShowRowDetailAsync()
    {
        if (_selectedRow is null)
        {
            await _dialogs.AlertAsync("提示", "请先在记录里选中一行。");
            return;
        }

        var row = _selectedRow;
        var check = row.Status == "完成" ? (row.Matched == true ? "一致" : "不一致") : "—";
        var meta = new List<KeyValuePair<string, string>>
        {
            new("方向", row.Direction),
            new("对端", string.IsNullOrEmpty(row.Remote) ? "—" : row.Remote),
            new("大小", Formats.HumanBytes(row.Size)),
            new("状态", row.Status),
            new("校验", check),
        };

        string? copyPath = null;
        if (row.Direction is "接收" or "推送")
        {
            copyPath = Path.Combine(CurrentReceiveDir(), row.Name);
            meta.Add(new KeyValuePair<string, string>("完整路径", copyPath));
        }

        await _dialogs.ShowFileDetailAsync(new FileDetailInfo
        {
            Title = "记录详情",
            Name = row.Name,
            Meta = meta,
            CopyPath = copyPath,
            HashRow = row,
        });
    }

    private async Task ShowFileDetailAsync()
    {
        var entry = ReceivedFiles.FirstOrDefault(item => (SelectedRow is null && false) || false)
                    ?? _selectedReceived;
        if (entry is null)
        {
            return;
        }

        await _dialogs.ShowFileDetailAsync(new FileDetailInfo
        {
            Title = "文件详情",
            Name = entry.Name,
            Meta = new List<KeyValuePair<string, string>>
            {
                new("大小", entry.SizeText),
                new("修改时间", entry.MtimeText),
                new("完整路径", entry.FullPath),
            },
            CopyPath = entry.FullPath,
        });
    }

    private FileEntry? _selectedReceived;

    /// <summary>接收列表点选一项时由页面调用。</summary>
    public void SelectReceivedFile(FileEntry? entry) => _selectedReceived = entry;

    public async Task ShowReceivedDetailAsync(FileEntry entry)
    {
        _selectedReceived = entry;
        await ShowFileDetailAsync();
    }

    // ------------------------------------------------------------------ #
    // 聊天
    // ------------------------------------------------------------------ #

    private void OnPeerText(string text, long ts, string peer)
    {
        AppendChat(text, fromPeer: true, system: false, quiet: false, ts: ts, sender: peer);
    }

    private void OnPeerNotice(string text, long ts, string peer)
    {
        AppendChat(text, fromPeer: true, system: true, quiet: false, ts: ts);
    }

    private async Task SendChatAsync()
    {
        var text = (ChatInput ?? "").Trim();
        if (text.Length == 0)
        {
            return;
        }

        var targets = TargetPeerLinks();
        if (targets.Count == 0)
        {
            return;
        }

        var sent = 0;
        var failures = new List<string>();
        foreach (var (_, link) in targets)
        {
            try
            {
                link.SendText(text);
                sent++;
            }
            catch (Exception ex) when (ex is IOException or ProtocolException or ArgumentException or SocketException)
            {
                failures.Add(ex.Message);
            }
        }

        if (sent == 0)
        {
            Log("发送失败：" + (failures.Count > 0 ? string.Join("；", failures) : "没有可用连接"));
            return;
        }

        string label;
        if (targets.Count == 1)
        {
            label = _role == "client" ? "我" : $"我 → {PeerLabel(targets[0].Key, withAddress: false)}";
        }
        else
        {
            label = $"我（{(sent == _controls.Count ? "群发" : "发送")} {sent} 人）";
        }

        AppendChat(text, fromPeer: false, system: false, quiet: false, ts: 0, sender: label);
        ChatInput = "";
        (SendChatCommand as AsyncRelayCommand)?.RaiseCanExecuteChanged();
    }

    private void AppendChat(string text, bool fromPeer, bool system, bool quiet, long ts, string? sender = null)
    {
        var body = text ?? "";
        if (fromPeer && _role == "server" && !string.IsNullOrEmpty(sender))
        {
            var alias = AliasForPeer(sender!);
            if (!string.IsNullOrEmpty(alias))
            {
                sender = alias;
            }
        }

        if (system)
        {
            foreach (var mark in new[] { "✔", "✗", "✓", "✘" })
            {
                body = body.Replace(" " + mark, "").Replace(mark, "");
            }

            body = body.Trim();
        }

        var entry = new ChatEntry
        {
            Time = NowStamp(ts),
            Who = system ? "系统" : (sender ?? (fromPeer ? "对方" : "我")),
            Text = body,
            Kind = system ? ChatKind.System : (fromPeer ? ChatKind.Peer : ChatKind.Me),
        };

        Chat.Add(entry);
        ChatAppended?.Invoke(this, EventArgs.Empty);

        if (fromPeer && !system && !quiet && !IsChatTab)
        {
            ChatTitle = "聊天 ●";
        }
    }

    private void SendNotice(string key, string text)
    {
        var targets = new List<ControlLink>();
        if (!string.IsNullOrEmpty(key))
        {
            if (_controls.TryGetValue(key, out var link))
            {
                targets.Add(link);
            }
        }
        else
        {
            targets.AddRange(_controls.Values);
        }

        foreach (var link in targets)
        {
            try
            {
                link.SendNotice(text);
            }
            catch (Exception)
            {
                // 通知失败不影响传输结果
            }
        }
    }

    // ------------------------------------------------------------------ #
    // 别名
    // ------------------------------------------------------------------ #

    private async Task SetAliasAsync()
    {
        if (_role != "server")
        {
            return;
        }

        if (_selectedPeer is null)
        {
            await _dialogs.AlertAsync("设置别名", "请先在线客户端列表里选中一个客户端。");
            return;
        }

        var key = _selectedPeer.Key;
        var peer = _controlPeers.GetValueOrDefault(key, key);
        var address = PeerFormatter.IpOf(peer);
        var current = _clientAliases.GetValueOrDefault(address, "");

        var alias = await _dialogs.PromptAsync(
            "设置客户端别名",
            $"客户端：{peer}\n输入便于识别的别名（如：我的手机 / 小明电脑）：",
            current,
            multiline: false,
            accept: "保存",
            secondaryLabel: "清除别名");

        if (alias is null)
        {
            return;
        }

        alias = alias.Trim();
        if (alias.Length > 32)
        {
            alias = alias[..32];
        }

        if (alias.Length > 0)
        {
            _clientAliases[address] = alias;
            Log($"已为客户端 {peer} 设置别名：{alias}");
        }
        else
        {
            _clientAliases.Remove(address);
            Log($"已清除客户端 {peer} 的别名。");
        }

        RefreshPeersView();
        RefreshRemoteRows(key, address);
    }

    private void RefreshRemoteRows(string key, string address)
    {
        foreach (var row in _rows.Values)
        {
            var clientId = row.ServerClientId;
            var peer = row.ServerPeer;
            if (clientId == key || (!string.IsNullOrEmpty(peer) && PeerFormatter.IpOf(peer) == address))
            {
                var source = !string.IsNullOrEmpty(peer) ? peer : _controlPeers.GetValueOrDefault(key, "");
                row.Remote = RemoteLabel(source, string.IsNullOrEmpty(clientId) ? key : clientId);
            }
        }
    }

    // ------------------------------------------------------------------ #
    // 地址与连接配置串
    // ------------------------------------------------------------------ #

    private void RefreshAddresses()
    {
        List<Ipv6AddressEntry> entries;
        try
        {
            entries = Ipv6Addresses.List();
        }
        catch (Exception ex)
        {
            entries = new List<Ipv6AddressEntry>();
            Log($"枚举 IPv6 地址失败：{ex.Message}");
        }

        Addresses.Clear();
        foreach (var entry in entries.Take(6))
        {
            Addresses.Add(new AddressItem { Entry = entry, Display = entry.Address });
        }

        SelectedAddress = null;
        if (Addresses.Count == 0)
        {
            Addresses.Add(new AddressItem
            {
                Entry = new Ipv6AddressEntry { Address = "未检测到 IPv6 地址（请确认 Wi-Fi 已开启 IPv6）" },
                Display = "未检测到 IPv6 地址（请确认 Wi-Fi 已开启 IPv6）",
            });
            Log("未检测到本机 IPv6 地址：请确认手机 Wi-Fi 有 IPv6；必要时在「设置→WLAN→当前网络」查看。");
        }
    }

    private async Task CopyAddressAsync()
    {
        if (_selectedAddress is null)
        {
            return;
        }

        var value = _selectedAddress.Address;
        if (value.Contains(':') && !value.Contains("未检测"))
        {
            if (await _dialogs.CopyAsync(value))
            {
                Log($"已复制地址：{value}");
            }
        }
    }

    private async Task ToggleConnectionConfigAsync()
    {
        if (_role == "server")
        {
            await ShareConnectionConfigAsync();
        }
        else
        {
            await ImportConnectionConfigAsync();
        }
    }

    private (string Host, string Scope) PickShareHost()
    {
        if (_selectedAddress is not null && !_selectedAddress.Address.Contains("未检测"))
        {
            return (_selectedAddress.Address, _selectedAddress.Entry.Scope);
        }

        foreach (var scope in new[] { "global", "ula" })
        {
            foreach (var item in Addresses)
            {
                if (item.Entry.Scope == scope && !item.Entry.Deprecated)
                {
                    return (item.Address, scope);
                }
            }
        }

        foreach (var item in Addresses)
        {
            if (item.Entry.Scope == "link-local")
            {
                return (item.Address, "link-local");
            }
        }

        return ("", "");
    }

    private async Task ShareConnectionConfigAsync()
    {
        var (host, scope) = PickShareHost();
        if (string.IsNullOrEmpty(host))
        {
            var manual = await _dialogs.PromptAsync(
                "手动输入本机地址",
                "没有自动检测到本机 IPv6 地址。\n请在手机「设置 → WLAN → 当前网络」查看 IPv6 地址并输入：",
                "",
                multiline: false,
                accept: "确定");
            if (string.IsNullOrEmpty(manual))
            {
                return;
            }

            var entry = Ipv6Addresses.Parse(manual);
            if (entry is null)
            {
                await _dialogs.AlertAsync("地址无效", "请输入有效的 IPv6 地址，例如 2409:xxxx::1");
                return;
            }

            host = entry.Address;
            scope = entry.Scope;
        }

        if (host.Split('%')[0] == "::1")
        {
            await _dialogs.AlertAsync("只有回环地址", "::1 只能本机使用，其他设备无法连接。");
            return;
        }

        string token;
        int port;
        try
        {
            port = int.Parse((Port ?? "9000").Trim());
            token = ConnectionConfigCodec.Encode(host, port, Password ?? "");
        }
        catch (Exception ex)
        {
            await _dialogs.AlertAsync("生成失败", ex.Message);
            return;
        }

        var hint = "包含地址、端口和密码，请私发。";
        hint += "\n如果地址列表里有多个全局地址，优先选稳定的那个（临时地址会变）。";
        if (scope == "link-local")
        {
            hint += "\n当前是链路本地地址，只有同一网段可用。";
        }

        await _dialogs.ShowShareConfigAsync(token, hint);
        Log($"已生成连接配置串：{host}，端口 {port}" + (string.IsNullOrEmpty(Password) ? "" : "（含密码）") + "。");
    }

    private async Task ImportConnectionConfigAsync()
    {
        var clipboard = await _dialogs.ReadClipboardAsync() ?? "";
        var sources = new List<string> { clipboard, GetChatPlainText() };

        foreach (var source in sources)
        {
            if (string.IsNullOrWhiteSpace(source))
            {
                continue;
            }

            try
            {
                var data = ConnectionConfigCodec.Decode(source);
                ApplyImportedConfig(data);
                return;
            }
            catch (ConnectionConfigException)
            {
                // 换下一个来源
            }
        }

        var initial = clipboard;
        var hint = string.IsNullOrEmpty(clipboard)
            ? "没有读到剪贴板内容：请长按输入框粘贴，或点「从文件导入」；也可以让服务器把连接串发到聊天里再点导入。"
            : "已从剪贴板读到内容；如果不是完整配置串，请重新粘贴。";
        var text = await _dialogs.ShowImportConfigAsync(initial, hint);
        if (string.IsNullOrEmpty(text))
        {
            return;
        }

        try
        {
            ApplyImportedConfig(ConnectionConfigCodec.Decode(text));
        }
        catch (ConnectionConfigException ex)
        {
            await _dialogs.AlertAsync("导入失败", ex.Message);
        }
    }

    private string GetChatPlainText()
    {
        var builder = new StringBuilder();
        foreach (var entry in Chat)
        {
            builder.AppendLine(entry.Display);
        }

        return builder.ToString();
    }

    private void ApplyImportedConfig(ConnectionConfigInfo data)
    {
        Host = data.Host;
        Port = data.Port.ToString();
        Password = data.Password;
        Zone = "";
        var suffix = string.IsNullOrEmpty(data.Password) ? "" : "（含连接密码）";
        Log($"已导入连接配置：{data.Host}，端口 {data.Port}{suffix}。点「连接」即可。");
        if (_controls.Count > 0)
        {
            Log("当前已连接，请先断开再重新连接以应用新配置。");
        }
    }

    // ------------------------------------------------------------------ #
    // 存储权限
    // ------------------------------------------------------------------ #

    private void RefreshStorageWarning()
    {
        var granted = MobileStorage.HasPublicStorageAccess;
        ShowStorageWarning = !granted;
        StorageWarningText = granted
            ? ""
            : "未获得公共存储权限：文件会保存到应用私有目录（卸载即丢失，其他文件管理器也看不到）。点右侧按钮授权后即可写入 Download/IPv6Transfer。";
    }

    private async Task RequestStorageAsync()
    {
        var granted = await MobileStorage.RequestPublicStorageAccessAsync();
        if (!granted)
        {
            await _dialogs.AlertAsync(
                "未获得授权",
                "请到「设置 → 应用 → 特殊权限 → 所有文件访问权限」里为本应用打开开关，然后回到应用刷新。\n"
                + "也可以继续使用应用私有目录。");
        }

        var defaultServer = MobileStorage.DefaultDirectory("received");
        var defaultClient = MobileStorage.DefaultDirectory("downloads");
        if (granted)
        {
            ServerReceiveDir = defaultServer;
            ClientReceiveDir = defaultClient;
            RefreshLocalList(true);
        }

        RefreshStorageWarning();
    }

    // ------------------------------------------------------------------ #
    // 配置持久化 / 退出
    // ------------------------------------------------------------------ #

    public void SaveConfig()
    {
        _config.Role = _role;
        _config.Port = Port ?? "9000";
        _config.Host = Host ?? "::1";
        _config.Zone = Zone ?? "";
        _config.Password = Password ?? "";
        _config.RecvDir = ServerReceiveDir;
        _config.ClientRecvDir = ClientReceiveDir;
        _config.ClientAliases = new Dictionary<string, string>(_clientAliases);
        _config.Save();
    }

    public void OnSleep()
    {
        SaveConfig();
    }

    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }

        _disposed = true;
        SaveConfig();

        try
        {
            _autoRefreshCts?.Cancel();
        }
        catch (Exception)
        {
            // 忽略
        }

        CancelTransfer();
        DisconnectPeer(silent: true);
        StopServer(silent: true);
    }
}
