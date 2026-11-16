// RevitWy.Plugin — 协议层：与 Python MCP Server 之间的 NDJSON 消息定义
// 目标框架：Revit 2022+（2022-2024 → net48；2025+ → net8.0-windows）
using System;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace RevitWy.Plugin
{
    /// <summary>请求信封：{"protocol":"revit-wy/1","id":"..","method":"..","params":{..}}</summary>
    public class RpcRequest
    {
        [JsonProperty("protocol")] public string Protocol { get; set; }
        [JsonProperty("id")] public string Id { get; set; }
        [JsonProperty("method")] public string Method { get; set; }
        [JsonProperty("params")] public JObject Params { get; set; }
    }

    /// <summary>响应信封：{"id":"..","result":{..}} 或 {"id":"..","error":{code,message}}</summary>
    public class RpcResponse
    {
        [JsonProperty("id")] public string Id { get; set; }
        [JsonProperty("result")] public object Result { get; set; }
        [JsonProperty("error")] public RpcError Error { get; set; }

        public string ToJson()
        {
            return JsonConvert.SerializeObject(this, Formatting.None);
        }

        public static RpcResponse Ok(string id, object result)
        {
            return new RpcResponse { Id = id, Result = result };
        }

        public static RpcResponse Fail(string id, string code, string message)
        {
            return new RpcResponse
            {
                Id = id,
                Error = new RpcError { Code = code, Message = message }
            };
        }
    }

    public class RpcError
    {
        [JsonProperty("code")] public string Code { get; set; }
        [JsonProperty("message")] public string Message { get; set; }
    }

    /// <summary>业务异常：带错误码，直接回传给客户端。</summary>
    public class RwyException : Exception
    {
        public string Code { get; private set; }
        public RwyException(string code, string message) : base(message) { Code = code; }
    }

    public static class ErrorCodes
    {
        public const string NoDocument = "NO_DOCUMENT";
        public const string BadParams = "BAD_PARAMS";
        public const string Unsupported = "UNSUPPORTED";
        public const string RevitApi = "REVIT_API";
        public const string Timeout = "TIMEOUT";
        public const string Internal = "INTERNAL";
    }
}
