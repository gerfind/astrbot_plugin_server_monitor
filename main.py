from astrbot.api.event import filter
from astrbot.api.star import Context, Star, register
from astrbot.api import logger,AstrBotConfig
import psutil
import platform
import datetime
import asyncio
from collections import deque
from typing import Optional
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
import io
import base64
import os
import matplotlib.dates as mdates


font_path = os.path.join(os.path.dirname(__file__), "SourceHanSansSC-Normal.otf")
fm.fontManager.addfont(font_path)

font_prop = fm.FontProperties(fname=font_path)
font_name = font_prop.get_name()

plt.rcParams["font.family"]        = font_name
plt.rcParams["font.sans-serif"]    = [font_name]
plt.rcParams["axes.unicode_minus"] = False

@register("server_status_monitor", "AoiInazuma", "服务器状态监控", "1.0", "https://github.com/gerfind/astrbot_plugin_server_monitor")
class ServerMonitor(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        #采样间隔 & 历史长度 & 默认时间 & 管理员模式
        self.sample_interval = config.get('sample_interval', 10)  # 秒
        max_history_min = config.get('max_history_minutes', 60)  # 分钟
        maxlen = int(max_history_min * 60 / self.sample_interval)
        self.default_time = config.get("default_time", 10)
        self.admin_only = config.get("admin_only",True)

        # 环形队列存储历史
        self.timestamps       = deque(maxlen=maxlen)
        self.cpu_history      = deque(maxlen=maxlen)
        self.mem_history      = deque(maxlen=maxlen)
        self.net_sent_history = deque(maxlen=maxlen)
        self.net_recv_history = deque(maxlen=maxlen)

        #告警配置
        alert_cfg = config.get('alert', {})
        thresh = alert_cfg.get('thresholds', {})
        self.cpu_threshold = thresh.get('cpu', 90.0)
        self.mem_threshold = thresh.get('mem', 90.0)
        self.net_threshold = thresh.get('net', 1024.0)   # KB/s
        self.load_threshold= thresh.get('load', os.cpu_count())
        self.alert_count   = alert_cfg.get('count', 3)

        # 启动后台采集
        self._monitor_task: Optional[asyncio.Task] = asyncio.create_task(self._monitor_loop())
        # 仅管理员模式
        if self.admin_only:
            self.server_status = filter.permission_type(filter.PermissionType.ADMIN)(self.server_status)

    def _get_uptime(self) -> str:
        boot = psutil.boot_time()
        now = datetime.datetime.now().timestamp()
        uptime_s = int(now - boot)
        d, rem = divmod(uptime_s, 86400)
        h, rem = divmod(rem, 3600)
        m, _   = divmod(rem, 60)
        parts = []
        if d: parts.append(f"{d}天")
        if h: parts.append(f"{h}小时")
        if m: parts.append(f"{m}分")
        return "".join(parts) or "0分"

    def conditional_decorator(decorator, condition):
        def _wrapper(func):
            return decorator(func) if condition else func
        return _wrapper

    async def _monitor_loop(self):
        last_net = psutil.net_io_counters()
        while True:
            await asyncio.sleep(self.sample_interval)
            cpu = psutil.cpu_percent(interval=None)
            mem = psutil.virtual_memory().percent

            net = psutil.net_io_counters()
            bs = net.bytes_sent - last_net.bytes_sent
            br = net.bytes_recv - last_net.bytes_recv
            last_net = net
            # KB/s
            kb_sent = bs / 1024 / self.sample_interval
            kb_recv = br / 1024 / self.sample_interval

            now = datetime.datetime.now()
            self.timestamps.append(now)
            self.cpu_history.append(cpu)
            self.mem_history.append(mem)
            self.net_sent_history.append(kb_sent)
            self.net_recv_history.append(kb_recv)

    @filter.command("状态查询", alias=["status"])
    async def server_status(self, event):
        """
        /status [minutes]
        绘制折线图 & 饼图，并基于 config.alert 判定是否告警。
        """
        try:
            text = event.message_str or ""
            parts = text.strip().split()
            minutes = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else self.default_time

            now = datetime.datetime.now()
            start = now - datetime.timedelta(minutes=minutes)

            data = [
                (t, cpu, mem, ns, nr)
                for t, cpu, mem, ns, nr in zip(
                    self.timestamps,
                    self.cpu_history,
                    self.mem_history,
                    self.net_sent_history,
                    self.net_recv_history
                ) if t >= start
            ]
            if not data:
                logger.info(f"⚠️ 最近 {minutes} 分钟无历史数据，请稍后再试。")
                yield event.plain_result(f"⚠️ 最近 {minutes} 分钟无历史数据，请稍后再试。")
                return

            times, cpus, mems, sends, recvs = zip(*data)
            uptime_str = self._get_uptime()

            # 绘图
            img_b64 = self._create_trend_and_load_image(times, cpus, mems, sends, recvs, uptime_str)
            img = base64.b64decode(img_b64)
            with open("status_trend.png", "wb") as f:
                f.write(img)
            yield event.image_result("status_trend.png")

            #——【4】阈值检测
            alerts = []
            if sum(1 for v in cpus  if v >= self.cpu_threshold) >= self.alert_count:
                alerts.append(f"CPU ≥ {self.cpu_threshold}% 共 {sum(1 for v in cpus if v >= self.cpu_threshold)} 次")
            if sum(1 for v in mems  if v >= self.mem_threshold) >= self.alert_count:
                alerts.append(f"内存 ≥ {self.mem_threshold}% 共 {sum(1 for v in mems if v >= self.mem_threshold)} 次")
            if sum(1 for v in sends if v >= self.net_threshold) >= self.alert_count:
                alerts.append(f"网络上行 ≥ {self.net_threshold} KB/s 共 {sum(1 for v in sends if v >= self.net_threshold)} 次")

            load1, load5, load15 = os.getloadavg()
            if load1 >= self.load_threshold:
                alerts.append(f"1min 负载 {load1:.2f} ≥ 阈值 {self.load_threshold}")

            for msg in alerts:
                yield event.plain_result(f"⚠️ 告警：{msg}")

        except Exception as e:
            yield event.plain_result(f"⚠️ 状态获取失败: {e}")

    def _create_trend_and_load_image(self, times, cpus, mems, sends, recvs, uptime_str) -> str:
        buf = io.BytesIO()
        fig = plt.figure(figsize=(8, 14))
        gs  = fig.add_gridspec(5, 3, height_ratios=[0.5, 2, 2, 2, 2])

        # 顶部：运行时间
        ax0 = fig.add_subplot(gs[0, :])
        ax0.axis("off")
        ax0.text(
            0.0, 0.5,
            f"系统已运行：{uptime_str}",
            ha="left", va="center",
            fontsize=12
        )

        # 中部折线图：占三列
        ax1 = fig.add_subplot(gs[1, :])
        ax1.plot(times, cpus);  ax1.set_ylabel("CPU (%)");  ax1.set_title("CPU 使用率")

        ax2 = fig.add_subplot(gs[2, :], sharex=ax1)
        ax2.plot(times, mems);  ax2.set_ylabel("内存 (%)");  ax2.set_title("内存使用率")

        ax3 = fig.add_subplot(gs[3, :], sharex=ax1)
        ax3.plot(times, sends, label="上行 (KB/s)")
        ax3.plot(times, recvs, label="下行 (KB/s)")
        ax3.set_ylabel("网络 (KB/s)")
        ax3.set_title("网络带宽")
        ax3.legend()

        # 底部饼图：1min/5min/15min
        load1, load5, load15 = os.getloadavg()
        loads = [load1, load5, load15]
        labels = ["1min", "5min", "15min"]
        cores = psutil.cpu_count(logical=True) or 1
        for i, (ld, lbl) in enumerate(zip(loads, labels)):
            ax = fig.add_subplot(gs[4, i])
            pct = min(ld / cores * 100, 100)
            ax.pie([pct, 100 - pct],
                   labels=[lbl, ""],
                   autopct='%1.1f%%',
                   startangle=90,
                   wedgeprops={'edgecolor': 'white', 'linewidth': 1})
            # 隐藏刻度与边框
            ax.set_xticks([]); ax.set_yticks([])
            for spine in ax.spines.values():
                spine.set_visible(False)
            ax.set_title(f"{lbl} 负载: {ld:.2f}")
        for ax in (ax1, ax2, ax3):
            ax.xaxis.set_major_locator(mdates.AutoDateLocator())
            ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
            for lbl in ax.get_xticklabels():
                lbl.set_rotation(45)
                lbl.set_ha('right')
        # 让刻度标签美观地倾斜
        plt.tight_layout()
        plt.savefig(buf, format="png", bbox_inches="tight")
        plt.clf()
        buf.seek(0)
        return base64.b64encode(buf.read()).decode("utf-8")

    async def terminate(self):
        if self._monitor_task and not self._monitor_task.cancelled():
            self._monitor_task.cancel()
        await super().terminate()
