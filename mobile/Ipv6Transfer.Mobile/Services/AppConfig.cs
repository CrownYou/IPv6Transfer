using System;
using System.Collections.Generic;
using System.IO;
using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace Ipv6Transfer.Mobile.Services;

/// <summary>
/// 应用配置，键名与 python 版 mobile_config.json 完全一致，
/// 因此两边的配置文件可以互相覆盖使用。
/// <para>
/// 读写全部基于 <see cref="JsonNode"/> / <see cref="Utf8JsonWriter"/>，
/// 不使用反射，Release 裁剪后也能正常工作。
/// </para>
/// </summary>
public sealed class AppConfig
{
    public string Role { get; set; } = "client";

    public string Port { get; set; } = "9000";

    public string Host { get; set; } = "::1";

    public string Zone { get; set; } = "";

    public string RecvDir { get; set; } = "";

    public string ClientRecvDir { get; set; } = "";

    public string Password { get; set; } = "";

    public Dictionary<string, string> ClientAliases { get; set; } = new();

    private static readonly JsonSerializerOptions WriteOptions = new()
    {
        WriteIndented = true,
        Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
    };

    public static string ConfigPath
    {
        get
        {
            try
            {
                return Path.Combine(FileSystem.AppDataDirectory, "mobile_config.json");
            }
            catch (Exception)
            {
                return Path.Combine(Path.GetTempPath(), "mobile_config.json");
            }
        }
    }

    public static AppConfig Load()
    {
        var config = new AppConfig();
        try
        {
            var path = ConfigPath;
            if (!File.Exists(path))
            {
                return config;
            }

            if (JsonNode.Parse(File.ReadAllText(path)) is not JsonObject root)
            {
                return config;
            }

            config.Role = ReadString(root, "role", "client");
            config.Port = ReadString(root, "port", "9000");
            config.Host = ReadString(root, "host", "::1");
            config.Zone = ReadString(root, "zone", "");
            config.RecvDir = ReadString(root, "recv_dir", "");
            config.ClientRecvDir = ReadString(root, "client_recv_dir", "");
            config.Password = ReadString(root, "password", "");

            if (root["client_aliases"] is JsonObject aliases)
            {
                foreach (var pair in aliases)
                {
                    if (pair.Value is not null && !string.IsNullOrWhiteSpace(pair.Key))
                    {
                        config.ClientAliases[pair.Key] = ReadValue(pair.Value);
                    }
                }
            }

            return config;
        }
        catch (Exception)
        {
            return new AppConfig();
        }
    }

    public void Save()
    {
        try
        {
            var path = ConfigPath;
            var directory = Path.GetDirectoryName(path);
            if (!string.IsNullOrEmpty(directory))
            {
                Directory.CreateDirectory(directory);
            }

            var root = new JsonObject
            {
                ["role"] = Role,
                ["port"] = Port,
                ["host"] = Host,
                ["zone"] = Zone,
                ["recv_dir"] = RecvDir,
                ["client_recv_dir"] = ClientRecvDir,
                ["password"] = Password,
            };

            var aliases = new JsonObject();
            foreach (var pair in ClientAliases)
            {
                aliases[pair.Key] = pair.Value;
            }

            root["client_aliases"] = aliases;
            File.WriteAllText(path, root.ToJsonString(WriteOptions));
        }
        catch (Exception)
        {
            // 配置保存失败不影响使用
        }
    }

    private static string ReadString(JsonObject root, string key, string fallback)
    {
        if (root[key] is not JsonValue value)
        {
            return fallback;
        }

        try
        {
            return value.TryGetValue<string>(out var text) && text is not null ? text : fallback;
        }
        catch (Exception)
        {
            return fallback;
        }
    }

    /// <summary>python 的 port 可能写成整数，这里统一转成字符串。</summary>
    private static string ReadValue(JsonNode node)
    {
        if (node is not JsonValue value)
        {
            return node.ToJsonString().Trim('"');
        }

        if (value.TryGetValue<string>(out var text) && text is not null)
        {
            return text;
        }

        if (value.TryGetValue<long>(out var number))
        {
            return number.ToString();
        }

        return value.ToJsonString().Trim('"');
    }
}
