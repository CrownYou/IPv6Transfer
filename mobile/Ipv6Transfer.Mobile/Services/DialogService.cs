using System;
using System.Collections.Generic;
using System.Threading.Tasks;
using Ipv6Transfer.Mobile.Models;
using Ipv6Transfer.Mobile.Views;
using Microsoft.Maui.ApplicationModel.DataTransfer;

namespace Ipv6Transfer.Mobile.Services;

/// <summary>文件/记录详情弹窗需要的全部内容。</summary>
public sealed class FileDetailInfo
{
    public required string Title { get; init; }
    public required string Name { get; init; }
    public required IReadOnlyList<KeyValuePair<string, string>> Meta { get; init; }
    public string? CopyPath { get; init; }
    public TransferRow? HashRow { get; init; }
}

/// <summary>界面需要的弹窗 / 剪贴板能力（由页面实现，ViewModel 只依赖接口）。</summary>
public interface IDialogService
{
    Task AlertAsync(string title, string message, string ok = "好");

    Task<bool> ConfirmAsync(string title, string message, string accept, string cancel);

    Task<bool> CopyAsync(string? text);

    Task<string?> ReadClipboardAsync();

    Task<string?> PickPathAsync(bool directoryMode, string startPath);

    /// <summary>单行/多行输入；secondaryLabel 不为空时提供第二个按钮，点击返回空字符串。</summary>
    Task<string?> PromptAsync(
        string title,
        string prompt,
        string initial = "",
        bool multiline = false,
        string accept = "确定",
        string? secondaryLabel = null);

    Task ShowFileDetailAsync(FileDetailInfo info);

    Task ShowHashDetailAsync(TransferRow row);

    Task ShowShareConfigAsync(string token, string hint);

    Task<string?> ShowImportConfigAsync(string initial, string hint);
}

/// <summary>基于 MAUI 模态页面的实现。</summary>
public sealed class DialogService : IDialogService
{
    public static DialogService Instance { get; } = new();

    private DialogService()
    {
    }

    /// <summary>由 MainPage 在 OnAppearing 里设置，用来拿到当前导航栈。</summary>
    public Func<Page?>? PageProvider { get; set; }

    private Page? Current => PageProvider?.Invoke();

    public Task AlertAsync(string title, string message, string ok = "好")
    {
        var page = Current;
        if (page is null)
        {
            return Task.CompletedTask;
        }

        return MainThread.InvokeOnMainThreadAsync(() => page.DisplayAlertAsync(title, message, ok));
    }

    public Task<bool> ConfirmAsync(string title, string message, string accept, string cancel)
    {
        var page = Current;
        if (page is null)
        {
            return Task.FromResult(false);
        }

        return MainThread.InvokeOnMainThreadAsync(() => page.DisplayAlertAsync(title, message, accept, cancel));
    }

    public async Task<bool> CopyAsync(string? text)
    {
        if (string.IsNullOrEmpty(text))
        {
            return false;
        }

        try
        {
            await Clipboard.Default.SetTextAsync(text);
            return true;
        }
        catch (Exception)
        {
            return false;
        }
    }

    public async Task<string?> ReadClipboardAsync()
    {
        try
        {
            var text = await Clipboard.Default.GetTextAsync();
            return string.IsNullOrWhiteSpace(text) ? null : text;
        }
        catch (Exception)
        {
            return null;
        }
    }

    public async Task<string?> PickPathAsync(bool directoryMode, string startPath)
    {
        var page = new FileBrowserPage(directoryMode, startPath);
        await PushAsync(page);
        return await page.Result;
    }

    public async Task<string?> PromptAsync(
        string title,
        string prompt,
        string initial = "",
        bool multiline = false,
        string accept = "确定",
        string? secondaryLabel = null)
    {
        var page = new PromptPage(title, prompt, initial, multiline, accept, secondaryLabel);
        await PushAsync(page);
        return await page.Result;
    }

    public async Task ShowFileDetailAsync(FileDetailInfo info)
    {
        var page = new DetailPage(info);
        await PushAsync(page);
        await page.Closed;
    }

    public async Task ShowHashDetailAsync(TransferRow row)
    {
        var page = new HashDetailPage(row);
        await PushAsync(page);
        await page.Closed;
    }

    public async Task ShowShareConfigAsync(string token, string hint)
    {
        var page = new ShareConfigPage(token, hint);
        await PushAsync(page);
        await page.Closed;
    }

    public async Task<string?> ShowImportConfigAsync(string initial, string hint)
    {
        var page = new ImportConfigPage(initial, hint);
        await PushAsync(page);
        return await page.Result;
    }

    private async Task PushAsync(Page page)
    {
        var host = Current;
        if (host is null)
        {
            return;
        }

        await MainThread.InvokeOnMainThreadAsync(async () => await host.Navigation.PushModalAsync(page));
    }
}
