// RevitWy.Plugin — 外部事件桥：把网络线程上的请求安全地排到 Revit UI 线程执行
using System;
using System.Collections.Concurrent;
using System.Threading;
using Autodesk.Revit.UI;

namespace RevitWy.Plugin
{
    /// <summary>
    /// Revit API 不是线程安全的，只能在 UI 线程调用。
    /// 因此：Socket 线程只负责收包 → 投递到队列 → Raise() 触发 ExternalEvent
    /// → Revit 回调里取任务执行 → 用 ManualResetEventSlim 回填结果。
    /// </summary>
    public sealed class ExternalEventBridge : IExternalEventHandler
    {
        private sealed class Job
        {
            public Func<UIApplication, object> Work;
            public object Result;
            public Exception Error;
            public readonly ManualResetEventSlim Done = new ManualResetEventSlim(false);
        }

        private readonly ConcurrentQueue<Job> _queue = new ConcurrentQueue<Job>();
        private readonly ExternalEvent _event;
        private UIApplication _uiApp;

        public ExternalEventBridge()
        {
            _event = ExternalEvent.Create(this);
        }

        public void Attach(UIApplication uiApp)
        {
            _uiApp = uiApp;
        }

        /// <summary>在任意线程调用；阻塞直到 UI 线程执行完（或超时）。</summary>
        public object Invoke(Func<UIApplication, object> work, TimeSpan timeout)
        {
            var job = new Job { Work = work };
            _queue.Enqueue(job);
            _event.Raise();

            if (!job.Done.Wait(timeout))
                throw new RwyException(ErrorCodes.Timeout,
                    "Revit 未在 " + timeout.TotalSeconds + "s 内完成该操作（可能弹出了模态对话框）");

            if (job.Error != null) throw job.Error;
            return job.Result;
        }

        public void Execute(UIApplication app)
        {
            _uiApp = app;
            Job job;
            while (_queue.TryDequeue(out job))
            {
                try
                {
                    job.Result = job.Work(app);
                }
                catch (Exception ex)
                {
                    job.Error = ex;
                }
                finally
                {
                    job.Done.Set();
                }
            }
        }

        public string GetName()
        {
            return "RevitWy.ExternalEventBridge";
        }
    }
}
