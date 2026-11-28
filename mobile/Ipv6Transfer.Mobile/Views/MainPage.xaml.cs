using System;
using System.Linq;
using Ipv6Transfer.Mobile.Models;
using Ipv6Transfer.Mobile.Services;
using Ipv6Transfer.Mobile.ViewModels;
using Microsoft.Maui.ApplicationModel;

namespace Ipv6Transfer.Mobile.Views;

public partial class MainPage : ContentPage
{
    private readonly MainViewModel _vm;
    private bool _dialogOpen;

    public MainPage(MainViewModel viewModel)
    {
        InitializeComponent();
        _vm = viewModel;
        BindingContext = viewModel;
        _vm.ChatAppended += OnChatAppended;
    }

    protected override void OnAppearing()
    {
        base.OnAppearing();
        DialogService.Instance.PageProvider = () => this;
        ScrollChatToEnd();
    }

    private void OnChatAppended(object? sender, EventArgs e) => ScrollChatToEnd();

    private void ScrollChatToEnd()
    {
        MainThread.BeginInvokeOnMainThread(() =>
        {
            try
            {
                var last = _vm.Chat.LastOrDefault();
                if (last is not null)
                {
                    ChatList.ScrollTo(last, position: ScrollToPosition.End, animate: false);
                }
            }
            catch (Exception)
            {
                // 列表还没渲染好时忽略
            }
        });
    }

    private async void OnHistorySelectionChanged(object? sender, SelectionChangedEventArgs e)
    {
        if (_dialogOpen)
        {
            return;
        }

        if (e.CurrentSelection.FirstOrDefault() is not TransferRow row)
        {
            return;
        }

        _dialogOpen = true;
        try
        {
            _vm.SelectedRow = row;
            await _vm.ShowRowDetailAsync();
        }
        finally
        {
            _dialogOpen = false;
        }
    }

    private async void OnReceiveSelectionChanged(object? sender, SelectionChangedEventArgs e)
    {
        if (_dialogOpen)
        {
            return;
        }

        if (e.CurrentSelection.FirstOrDefault() is not FileEntry entry)
        {
            return;
        }

        _dialogOpen = true;
        try
        {
            ReceiveList.SelectedItem = null;
            await _vm.ShowReceivedDetailAsync(entry);
        }
        finally
        {
            _dialogOpen = false;
        }
    }

    protected override bool OnBackButtonPressed()
    {
        // 不在「连接」页时，返回键先回到连接页，再按一次才退出
        if (!_vm.IsConnectTab)
        {
            _vm.SelectTabCommand.Execute("conn");
            return true;
        }

        return base.OnBackButtonPressed();
    }
}
