// RevitWy.Plugin — TCP 服务：监听 localhost，收 NDJSON，派发，回包
using System;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace RevitWy.Plugin
{
    /// <summary>
    /// 单连接、串行处理的简单服务器。
    /// 为什么不是多连接：Revit 只有一个当前文档，并发请求没有意义且容易踩事务冲突；
    /// 串行 + ExternalEvent 排队反而最稳。若客户端断线，自动回到监听状态。
    /// </summary>
    public sealed class SocketServer : IDisposable
    {
        private readonly int _port;
        private readonly Func<string, JObject, object> _dispatcher;
        private TcpListener _listener;
        private CancellationTokenSource _cts;
        private Task _acceptTask;
        private volatile bool _running;

        public string LastError { get; private set; }
        public bool IsRunning { get { return _running; } }
        public int Port { get { return _port; } }
        public bool ClientConnected { get; private set; }

        public SocketServer(int port, Func<string, JObject, object> dispatcher)
        {
            _port = port;
            _dispatcher = dispatcher;
            LastError = null;
        }

        public void Start()
        {
            Stop();
            try
            {
                _listener = new TcpListener(IPAddress.Loopback, _port);
                _listener.Start();
                _cts = new CancellationTokenSource();
                _running = true;
                _acceptTask = Task.Run(() => AcceptLoop(_cts.Token));
            }
            catch (Exception ex)
            {
                _running = false;
                LastError = "端口 " + _port + " 启动失败：" + ex.Message
                          + "（可能被其他程序占用，可用 revit-wy 命令的 --port 换端口）";
                throw new RwyException(ErrorCodes.Internal, LastError);
            }
        }

        public void Stop()
        {
            _running = false;
            try { if (_cts != null) _cts.Cancel(); } catch { }
            try { if (_listener != null) _listener.Stop(); } catch { }
            _listener = null;
            ClientConnected = false;
        }

        private async Task AcceptLoop(CancellationToken token)
        {
            while (!token.IsCancellationRequested)
            {
                TcpClient client = null;
                try
                {
                    client = await _listener.AcceptTcpClientAsync().ConfigureAwait(false);
                }
                catch (ObjectDisposedException) { return; }
                catch (SocketException) { if (!_running) return; continue; }

                ClientConnected = true;
                try
                {
                    client.NoDelay = true;
                    using (var stream = client.GetStream())
                    using (var reader = new StreamReader(stream, new UTF8Encoding(false)))
                    using (var writer = new StreamWriter(stream, new UTF8Encoding(false)))
                    {
                        writer.AutoFlush = true;
                        string line;
                        while ((line = await reader.ReadLineAsync().ConfigureAwait(false)) != null)
                        {
                            if (line.Trim().Length == 0) continue;
                            string reply = HandleLine(line);
                            if (reply != null)
                            {
                                await writer.WriteLineAsync(reply).ConfigureAwait(false);
                            }
                        }
                    }
                }
                catch (Exception ex)
                {
                    LastError = ex.Message;
                }
                finally
                {
                    ClientConnected = false;
                    try { client.Close(); } catch { }
                }
            }
        }

        private string HandleLine(string line)
        {
            RpcRequest req;
            try
            {
                req = JsonConvert.DeserializeObject<RpcRequest>(line);
            }
            catch (Exception ex)
            {
                return RpcResponse.Fail(null, ErrorCodes.BadParams,
                    "JSON 解析失败: " + ex.Message).ToJson();
            }

            if (req == null || string.IsNullOrEmpty(req.Method))
                return RpcResponse.Fail(req == null ? null : req.Id,
                    ErrorCodes.BadParams, "缺少 method 字段").ToJson();

            try
            {
                object result = _dispatcher(req.Method, req.Params ?? new JObject());
                return RpcResponse.Ok(req.Id, result).ToJson();
            }
            catch (RwyException rex)
            {
                return RpcResponse.Fail(req.Id, rex.Code, rex.Message).ToJson();
            }
            catch (Exception ex)
            {
                return RpcResponse.Fail(req.Id, ErrorCodes.Internal,
                    ex.GetType().Name + ": " + ex.Message).ToJson();
            }
        }

        public JObject Status()
        {
            return JObject.FromObject(new
            {
                running = _running,
                port = _port,
                client_connected = ClientConnected,
                last_error = LastError
            });
        }

        public void Dispose()
        {
            Stop();
            if (_acceptTask != null)
            {
                try { _acceptTask.Wait(1000); } catch { }
            }
        }
    }
}
