using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Net;
using System.Text;
using System.Text.RegularExpressions;
using System.Web.Script.Serialization;

internal static class SalMekomiServer
{
    private const int DefaultPort = 8765;
    private static int Port = DefaultPort;
    private static readonly string Root = AppDomain.CurrentDomain.BaseDirectory;
    private static string BaseUrl;
    private static readonly Dictionary<string, List<string>> Runs =
        new Dictionary<string, List<string>>(StringComparer.Ordinal);
    private static readonly JavaScriptSerializer Json = new JavaScriptSerializer();
    private static HttpListener listener;

    private static int Main(string[] commandLine)
    {
        bool openBrowser = true;
        bool customPort = false;
        for (int i = 0; i < commandLine.Length; i++)
        {
            if (commandLine[i] == "--port" && i + 1 < commandLine.Length)
            {
                int port;
                if (!Int32.TryParse(commandLine[++i], out port) || port < 1024 || port > 65535)
                {
                    Console.Error.WriteLine("מספר היציאה אינו תקין.");
                    return 2;
                }
                Port = port;
                customPort = true;
            }
            else if (commandLine[i] == "--no-browser")
            {
                openBrowser = false;
            }
            else
            {
                Console.Error.WriteLine("אפשרויות: --port <1024-65535> --no-browser");
                return 2;
            }
        }
        int lastPort = customPort ? Port : DefaultPort + 15;
        while (Port <= lastPort)
        {
            BaseUrl = "http://127.0.0.1:" + Port + "/";
            listener = new HttpListener();
            listener.Prefixes.Add(BaseUrl);
            try
            {
                listener.Start();
                break;
            }
            catch (HttpListenerException)
            {
                listener.Close();
                Port++;
            }
        }
        if (listener == null || !listener.IsListening)
        {
            Console.Error.WriteLine("לא ניתן להפעיל את הרכיב המקומי ביציאות " + DefaultPort + "-" + lastPort + ".");
            return 1;
        }

        Console.WriteLine("סל מקומי פועל בכתובת " + BaseUrl);
        Console.WriteLine("השאירו את החלון פתוח. סגרו אותו כדי לעצור את הרכיב.");
        if (openBrowser) try
        {
            Process.Start(BaseUrl);
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("לא ניתן לפתוח דפדפן אוטומטית: " + ex.Message);
            Console.WriteLine("פתחו ידנית: " + BaseUrl);
        }

        Console.CancelKeyPress += delegate(object sender, ConsoleCancelEventArgs args)
        {
            args.Cancel = true;
            listener.Stop();
        };

        while (listener.IsListening)
        {
            HttpListenerContext context;
            try
            {
                context = listener.GetContext();
            }
            catch (HttpListenerException)
            {
                break;
            }
            catch (ObjectDisposedException)
            {
                break;
            }

            try
            {
                Handle(context);
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine(ex.Message);
                try
                {
                    SendJson(context, 500, new Dictionary<string, object> { { "error", ex.Message } });
                }
                catch
                {
                    context.Response.Close();
                }
            }
        }

        listener.Close();
        return 0;
    }

    private static void Handle(HttpListenerContext context)
    {
        string host = context.Request.Headers["Host"];
        if (host != "127.0.0.1:" + Port && host != "localhost:" + Port)
        {
            SendJson(context, 403, new Dictionary<string, object> { { "error", "כתובת מארח נדחתה." } });
            return;
        }

        string path = context.Request.Url.AbsolutePath;
        if (context.Request.HttpMethod == "GET" && (path == "/" || path == "/index.html"))
        {
            Send(context, 200, File.ReadAllBytes(Path.Combine(Root, "index.html")), "text/html; charset=utf-8");
            return;
        }
        if (context.Request.HttpMethod == "GET" && path == "/api/status")
        {
            SendJson(context, 200, new Dictionary<string, object>
            {
                { "ok", true },
                { "downloader", FindDownloader() != null }
            });
            return;
        }
        if (context.Request.HttpMethod == "POST" && path == "/api/download-all")
        {
            StartDownloader(context);
            return;
        }
        if (context.Request.HttpMethod == "GET" && path.StartsWith("/downloads/", StringComparison.Ordinal))
        {
            ServeDownloadedFile(context, path);
            return;
        }
        if (context.Request.HttpMethod == "POST" && path == "/api/cleanup")
        {
            CleanupRun(context);
            return;
        }

        SendJson(context, 404, new Dictionary<string, object> { { "error", "לא נמצא." } });
    }

    private static void StartDownloader(HttpListenerContext context)
    {
        if (context.Request.Headers["Origin"] != BaseUrl.TrimEnd('/'))
        {
            SendJson(context, 403, new Dictionary<string, object> { { "error", "בקשה ממקור אחר נדחתה." } });
            return;
        }
        if (context.Request.ContentType == null ||
            !context.Request.ContentType.StartsWith("application/json", StringComparison.OrdinalIgnoreCase))
        {
            SendJson(context, 400, new Dictionary<string, object> { { "error", "נדרשת בקשת JSON." } });
            return;
        }
        ReadJsonBody(context.Request);

        string exe = FindDownloader();
        if (exe == null)
        {
            SendJson(context, 404, new Dictionary<string, object>
            {
                { "error", "לא נמצא download-all-networks.exE. הציבו אותו ליד תיקיית sal-mekomi או בתוכה." }
            });
            return;
        }

        string downloadRoot = Path.Combine(Path.GetDirectoryName(exe), "downloads");
        Dictionary<string, string> before = Snapshot(downloadRoot);
        ProcessStartInfo startInfo = new ProcessStartInfo
        {
            FileName = exe,
            WorkingDirectory = Path.GetDirectoryName(exe),
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true
        };

        StringBuilder output = new StringBuilder();
        object outputLock = new object();
        using (Process process = new Process())
        {
            process.StartInfo = startInfo;
            process.OutputDataReceived += delegate(object sender, DataReceivedEventArgs args)
            {
                if (args.Data != null) lock (outputLock) output.AppendLine(args.Data);
            };
            process.ErrorDataReceived += delegate(object sender, DataReceivedEventArgs args)
            {
                if (args.Data != null) lock (outputLock) output.AppendLine(args.Data);
            };
            if (!process.Start())
            {
                throw new InvalidOperationException("לא ניתן להפעיל את קובץ ההורדה.");
            }
            process.StandardInput.Close();
            process.BeginOutputReadLine();
            process.BeginErrorReadLine();
            if (!process.WaitForExit(1200000))
            {
                process.Kill();
                throw new TimeoutException("ההורדה נמשכה יותר מ־20 דקות והופסקה.");
            }
            process.WaitForExit();

            List<string> files = ChangedFiles(downloadRoot, before);
            if (files.Count > 24)
            {
                SendJson(context, 413, new Dictionary<string, object>
                {
                    { "error", "מספר הקבצים שהורדו חורג מהצפוי; לא יובאו קבצים." }
                });
                return;
            }
            if (files.Count == 0)
            {
                string message = "לא נמצאו קבצים חדשים. בדקו חיבור לאינטרנט ואת הודעות ההורדה.";
                string text = Tail(output.ToString(), 12000);
                if (text.Length != 0) message += "\n" + text;
                SendJson(context, 502, new Dictionary<string, object> { { "error", message } });
                return;
            }

            string runId = Guid.NewGuid().ToString("N");
            Runs[runId] = files;
            SendJson(context, 200, new Dictionary<string, object>
            {
                { "runId", runId },
                { "files", files },
                { "exitCode", process.ExitCode },
                { "output", Tail(output.ToString(), 12000) }
            });
        }
    }

    private static Dictionary<string, string> Snapshot(string folder)
    {
        Dictionary<string, string> result = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        if (!Directory.Exists(folder)) return result;
        foreach (string path in Directory.GetFiles(folder))
        {
            FileInfo file = new FileInfo(path);
            if (!IsDataFile(file.Name)) continue;
            result[file.Name] = file.Length + ":" + file.LastWriteTimeUtc.Ticks;
        }
        return result;
    }

    private static List<string> ChangedFiles(string folder, Dictionary<string, string> before)
    {
        List<string> result = new List<string>();
        if (!Directory.Exists(folder)) return result;
        foreach (string path in Directory.GetFiles(folder))
        {
            FileInfo file = new FileInfo(path);
            if (!IsDataFile(file.Name)) continue;
            string stamp = file.Length + ":" + file.LastWriteTimeUtc.Ticks;
            string previous;
            if (!before.TryGetValue(file.Name, out previous) || previous != stamp)
            {
                result.Add(file.Name);
            }
        }
        result.Sort(StringComparer.OrdinalIgnoreCase);
        return result;
    }

    private static bool IsDataFile(string name)
    {
        return Regex.IsMatch(name, @"^[A-Za-z0-9_.-]+\.(?:gz|zip|xml)$", RegexOptions.IgnoreCase);
    }

    private static string FindDownloader()
    {
        string[] paths =
        {
            Path.Combine(Root, "download-all-networks.exE"),
            Path.Combine(Directory.GetParent(Root).FullName, "download-all-networks.exE")
        };
        return paths.FirstOrDefault(File.Exists);
    }

    private static void ServeDownloadedFile(HttpListenerContext context, string path)
    {
        string[] parts = path.Split(new[] { '/' }, StringSplitOptions.RemoveEmptyEntries);
        if (parts.Length != 3 || !Regex.IsMatch(parts[1], @"^[0-9a-f]{32}$"))
        {
            SendJson(context, 404, new Dictionary<string, object> { { "error", "קובץ ההורדה לא נמצא." } });
            return;
        }

        string name;
        try
        {
            name = Uri.UnescapeDataString(parts[2]);
        }
        catch (UriFormatException)
        {
            SendJson(context, 400, new Dictionary<string, object> { { "error", "שם קובץ לא תקין." } });
            return;
        }
        List<string> runFiles;
        if (!Runs.TryGetValue(parts[1], out runFiles) || !IsDataFile(name) ||
            !runFiles.Contains(name, StringComparer.OrdinalIgnoreCase))
        {
            SendJson(context, 404, new Dictionary<string, object> { { "error", "קובץ ההורדה לא נמצא." } });
            return;
        }

        string exe = FindDownloader();
        if (exe == null)
        {
            SendJson(context, 404, new Dictionary<string, object> { { "error", "קובץ ההורדה אינו זמין." } });
            return;
        }
        string file = Path.Combine(Path.GetDirectoryName(exe), "downloads", name);
        if (!File.Exists(file))
        {
            SendJson(context, 404, new Dictionary<string, object> { { "error", "קובץ ההורדה אינו זמין." } });
            return;
        }
        Send(context, 200, File.ReadAllBytes(file), "application/octet-stream");
    }

    private static void CleanupRun(HttpListenerContext context)
    {
        if (context.Request.Headers["Origin"] != BaseUrl.TrimEnd('/'))
        {
            SendJson(context, 403, new Dictionary<string, object> { { "error", "בקשה ממקור אחר נדחתה." } });
            return;
        }
        Dictionary<string, object> body = ReadJsonBody(context.Request);
        object runIdValue;
        string runId = body.TryGetValue("runId", out runIdValue) ? Convert.ToString(runIdValue) : "";
        if (Regex.IsMatch(runId, @"^[0-9a-f]{32}$")) Runs.Remove(runId);
        SendJson(context, 200, new Dictionary<string, object> { { "ok", true } });
    }

    private static Dictionary<string, object> ReadJsonBody(HttpListenerRequest request)
    {
        if (request.ContentLength64 < 1 || request.ContentLength64 > 2048)
        {
            throw new InvalidDataException("בקשה לא תקינה.");
        }
        using (StreamReader reader = new StreamReader(request.InputStream, request.ContentEncoding))
        {
            object parsed = Json.DeserializeObject(reader.ReadToEnd());
            Dictionary<string, object> result = parsed as Dictionary<string, object>;
            if (result == null) throw new InvalidDataException("נדרשת בקשת JSON תקינה.");
            return result;
        }
    }

    private static void SendJson(HttpListenerContext context, int status, object value)
    {
        Send(context, status, Encoding.UTF8.GetBytes(Json.Serialize(value)), "application/json; charset=utf-8");
    }

    private static void Send(HttpListenerContext context, int status, byte[] body, string contentType)
    {
        HttpListenerResponse response = context.Response;
        response.StatusCode = status;
        response.ContentType = contentType;
        response.Headers["X-Content-Type-Options"] = "nosniff";
        response.Headers["Cache-Control"] = "no-store";
        response.Headers["Referrer-Policy"] = "same-origin";
        response.ContentLength64 = body.Length;
        response.OutputStream.Write(body, 0, body.Length);
        response.Close();
    }

    private static string Tail(string value, int maxLength)
    {
        return value.Length <= maxLength ? value : value.Substring(value.Length - maxLength);
    }
}
