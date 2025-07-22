import shutil
import re
import tempfile
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
from astrbot.core.message.message_event_result import MessageChain
import psutil
import datetime
import asyncio
from collections import deque
from typing import Optional, List
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
import matplotlib.dates as mdates
import io
import os
import subprocess as sync_subprocess
from rich.console import Console
from rich.text import Text
import cairosvg
from PIL import Image

# —— 中文字体加载
font_path = os.path.join(os.path.dirname(__file__), "SourceHanSansSC-Normal.otf")
fm.fontManager.addfont(font_path)
font_name = fm.FontProperties(fname=font_path).get_name()
plt.rcParams["font.family"] = font_name
plt.rcParams["font.sans-serif"] = [font_name]
plt.rcParams["axes.unicode_minus"] = False

@register("server_status_monitor", "AoiInazuma", "服务器状态监控", "1.0", "https://github.com/gerfind/astrbot_plugin_server_monitor")
class ServerMonitor(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config

        # 验证配置
        self._validate_config(config)

        # 采样 & 队列
        self.sample_interval = max(1, config.get("sample_interval", 10))
        max_history_min = max(1, config.get("max_history_minutes", 60))
        maxlen = int(max_history_min * 60 / self.sample_interval)
        self.default_time = max(1, config.get("default_time", 10))
        self.admin_only = config.get("admin_only", True)
        self.alert_groups = config.get("alert_sender", [])
        self.services_list = self._validate_service_list(config.get("services_list", []))

        self.timestamps = deque(maxlen=maxlen)
        self.cpu_history = deque(maxlen=maxlen)
        self.mem_history = deque(maxlen=maxlen)
        self.net_sent_history = deque(maxlen=maxlen)
        self.net_recv_history = deque(maxlen=maxlen)
        self.load_history = deque(maxlen=maxlen)

        # 阈值
        alert_cfg = config.get("alert", {})
        thresh = alert_cfg.get("thresholds", {})
        self.cpu_thresh = max(0.0, min(100.0, thresh.get("cpu", 90.0)))
        self.mem_thresh = max(0.0, min(100.0, thresh.get("mem", 90.0)))
        self.net_thresh = max(0.0, thresh.get("net", 1024.0))
        cpu_count = os.cpu_count() or 1
        self.load_thresh = max(0.0, thresh.get("load", cpu_count))
        self.alert_count = max(1, alert_cfg.get("count", 3))
        self.alert_interval = max(1, config.get("alert_check_interval", 60))

        self._cpu_alerted = False
        self._mem_alerted = False
        self._net_alerted = False
        self._load_alerted = False

        # 后台任务
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        self._alert_task = asyncio.create_task(self._alert_loop())

        # 管理员模式保护
        if self.admin_only:
            self.server_status = filter.permission_type(filter.PermissionType.ADMIN)(self.server_status)

    def _validate_config(self, config: AstrBotConfig):
        """验证配置参数的合法性"""
        try:
            # 检查基本配置类型
            if not isinstance(config.get("admin_only", True), bool):
                logger.warning("配置 admin_only 应为布尔值，使用默认值 True")
            
            sample_interval = config.get("sample_interval", 10)
            if not isinstance(sample_interval, int) or sample_interval < 1:
                logger.warning("配置 sample_interval 应为正整数，使用默认值 10")
            
            max_history_minutes = config.get("max_history_minutes", 60)
            if not isinstance(max_history_minutes, int) or max_history_minutes < 1:
                logger.warning("配置 max_history_minutes 应为正整数，使用默认值 60")
            
            default_time = config.get("default_time", 10)
            if not isinstance(default_time, int) or default_time < 1:
                logger.warning("配置 default_time 应为正整数，使用默认值 10")
            
            alert_sender = config.get("alert_sender", [])
            if not isinstance(alert_sender, list):
                logger.warning("配置 alert_sender 应为列表，使用默认值 []")
            
            services_list = config.get("services_list", [])
            if not isinstance(services_list, list):
                logger.warning("配置 services_list 应为列表，使用默认值 []")
            
            # 检查告警配置
            alert_config = config.get("alert", {})
            if not isinstance(alert_config, dict):
                logger.warning("配置 alert 应为对象，使用默认值 {}")
                return
            
            thresholds = alert_config.get("thresholds", {})
            if not isinstance(thresholds, dict):
                logger.warning("配置 alert.thresholds 应为对象，使用默认值 {}")
                return
            
            # 检查各个阈值
            for key, default, min_val, max_val in [
                ("cpu", 90.0, 0.0, 100.0),
                ("mem", 90.0, 0.0, 100.0),
                ("net", 1024.0, 0.0, None),
                ("load", 1.5, 0.0, None)
            ]:
                value = thresholds.get(key, default)
                if not isinstance(value, (int, float)) or value < min_val:
                    logger.warning(f"配置 alert.thresholds.{key} 应为大于等于 {min_val} 的数值，使用默认值 {default}")
                elif max_val is not None and value > max_val:
                    logger.warning(f"配置 alert.thresholds.{key} 应为小于等于 {max_val} 的数值，使用默认值 {default}")
            
            count = alert_config.get("count", 3)
            if not isinstance(count, int) or count < 1:
                logger.warning("配置 alert.count 应为正整数，使用默认值 3")
                
        except Exception as e:
            logger.warning(f"配置验证失败: {e}，将使用默认配置")

    def _validate_service_list(self, services_list):
        """验证服务列表，确保只包含合法的服务名称"""
        if not isinstance(services_list, list):
            return []
        
        validated_services = []
        for service in services_list:
            if isinstance(service, str) and service.strip():
                # 移除可能的 .service 后缀
                service_name = service.strip()
                if service_name.endswith('.service'):
                    service_name = service_name[:-8]
                
                # 验证服务名称格式 (只允许字母、数字、下划线和连字符)
                import re
                if re.match(r'^[a-zA-Z0-9_-]+$', service_name):
                    validated_services.append(service_name)
                else:
                    logger.warning(f"无效的服务名称: {service}，已跳过")
            else:
                logger.warning(f"无效的服务配置项: {service}，已跳过")
        
        return validated_services

    @filter.command_group("server", alias={"监控"})
    def server_group(self):
        pass

    def _get_uptime(self) -> str:
        boot = psutil.boot_time()
        uptime = int(datetime.datetime.now().timestamp() - boot)
        d, rem = divmod(uptime, 86400)
        h, rem = divmod(rem, 3600)
        m, _ = divmod(rem, 60)
        parts = []
        if d: parts.append(f"{d}天")
        if h: parts.append(f"{h}小时")
        if m: parts.append(f"{m}分")
        return "".join(parts) or "0分"

    async def _monitor_loop(self):
        """后台监控循环，收集系统指标"""
        try:
            last_net = psutil.net_io_counters()
        except Exception as e:
            logger.error(f"初始化网络监控失败: {e}")
            # 使用默认值，避免程序崩溃
            last_net = None
            
        while True:
            await asyncio.sleep(self.sample_interval)
            try:
                # CPU 使用率
                cpu = psutil.cpu_percent(None)
                
                # 内存使用率
                mem = psutil.virtual_memory().percent
                
                # 网络速率计算
                if last_net is not None:
                    try:
                        net = psutil.net_io_counters()
                        bs = net.bytes_sent - last_net.bytes_sent
                        br = net.bytes_recv - last_net.bytes_recv
                        last_net = net
                        kb_sent = bs / 1024 / self.sample_interval
                        kb_recv = br / 1024 / self.sample_interval
                    except Exception as e:
                        logger.warning(f"获取网络数据失败: {e}")
                        kb_sent = kb_recv = 0
                else:
                    try:
                        last_net = psutil.net_io_counters()
                        kb_sent = kb_recv = 0
                    except Exception:
                        kb_sent = kb_recv = 0
                
                # 系统负载
                try:
                    load1, _, _ = os.getloadavg()
                except OSError:
                    load1 = 0.0
                
                # 记录数据
                now = datetime.datetime.now()
                self.timestamps.append(now)
                self.cpu_history.append(cpu)
                self.mem_history.append(mem)
                self.net_sent_history.append(kb_sent)
                self.net_recv_history.append(kb_recv)
                self.load_history.append(load1)
                
            except Exception as e:
                logger.error(f"监控循环出错: {e}")
                # 避免无限循环中的错误堆积
                await asyncio.sleep(1)

    async def _alert_loop(self):
        while True:
            await asyncio.sleep(self.alert_interval)
            alerts: List[str] = []
            # CPU
            if len(self.cpu_history) >= self.alert_count:
                recent = list(self.cpu_history)[-self.alert_count:]
                if all(v >= self.cpu_thresh for v in recent) and not self._cpu_alerted:
                    alerts.append(f"CPU 使用率连续 {self.alert_count} 次≥{self.cpu_thresh}%")
                    self._cpu_alerted = True
                elif not all(v >= self.cpu_thresh for v in recent):
                    self._cpu_alerted = False
            # 内存
            if len(self.mem_history) >= self.alert_count:
                recent = list(self.mem_history)[-self.alert_count:]
                if all(v >= self.mem_thresh for v in recent) and not self._mem_alerted:
                    alerts.append(f"内存使用率连续 {self.alert_count} 次≥{self.mem_thresh}%")
                    self._mem_alerted = True
                elif not all(v >= self.mem_thresh for v in recent):
                    self._mem_alerted = False
            # 网络
            if len(self.net_sent_history) >= self.alert_count:
                recent = list(self.net_sent_history)[-self.alert_count:]
                if all(v >= self.net_thresh for v in recent) and not self._net_alerted:
                    alerts.append(f"网络上行速率连续 {self.alert_count} 次≥{self.net_thresh} KB/s")
                    self._net_alerted = True
                elif not all(v >= self.net_thresh for v in recent):
                    self._net_alerted = False
            # 负载
            if len(self.load_history) >= self.alert_count:
                recent = list(self.load_history)[-self.alert_count:]
                if all(v >= self.load_thresh for v in recent) and not self._load_alerted:
                    alerts.append(f"1min 负载连续 {self.alert_count} 次≥{self.load_thresh}")
                    self._load_alerted = True
                elif not all(v >= self.load_thresh for v in recent):
                    self._load_alerted = False
            # 推送
            if alerts and self.alert_groups:
                text = "\n".join(f"⚠️ {msg}" for msg in alerts)
                for gid in self.alert_groups:
                    mc = MessageChain().message(text)
                    logger.info(f"[告警] 推送至 {gid}: {text}")
                    await self.context.send_message(gid, mc)

    @server_group.command("status")
    async def server_status(self, event, time: Optional[str] = None):
        try:
            minutes = self.default_time if time is None else int(time)
            start = datetime.datetime.now() - datetime.timedelta(minutes=minutes)
            data = [(t,c,m,s,r) for t,c,m,s,r in zip(
                self.timestamps, self.cpu_history, self.mem_history,
                self.net_sent_history, self.net_recv_history
            ) if t>=start]
            if not data:
                yield event.plain_result(f"⚠️ 最近 {minutes} 分钟无数据")
                return
            times, cpus, mems, sends, recvs = zip(*data)
            img_bytes = self._create_trend_image(times, cpus, mems, sends, recvs)
            
            # 使用临时文件并确保清理
            import tempfile
            with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp_file:
                tmp_file.write(img_bytes)
                tmp_path = tmp_file.name
            
            try:
                yield event.image_result(tmp_path)
            finally:
                # 确保临时文件被删除
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                    
        except ValueError:
            yield event.plain_result("⚠️ 时间参数应为正整数")
        except Exception as e:
            yield event.plain_result(f"⚠️ 状态获取失败: {e}")

    @server_group.command("service")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def server_service(self, event, service: Optional[str] = None):
        """
        /server service [服务名]
        获取 systemctl status 并直接输出 PNG/JPG
        """
        # 查找 systemctl 命令
        systemctl_paths = ["/usr/bin/systemctl", "/bin/systemctl", "/usr/local/bin/systemctl"]
        cmd = None
        for path in systemctl_paths:
            if os.path.isfile(path) and os.access(path, os.X_OK):
                cmd = path
                break
        
        if not cmd:
            yield event.plain_result("⚠️ 未找到 systemctl 命令，请检查系统环境")
            return
            
        services = [service] if service else self.services_list
        if not services:
            yield event.plain_result("⚠️ services_list 未配置，请在配置中添加要监控的服务")
            return
            
        for svc in services:
            try:
                # 执行 systemctl 命令
                out = sync_subprocess.check_output([cmd, "status", svc, "--no-pager"], 
                                                   stderr=sync_subprocess.STDOUT, timeout=10)
                text = out.decode(errors='ignore')
            except sync_subprocess.CalledProcessError as e:
                text = e.output.decode(errors='ignore')
            except sync_subprocess.TimeoutExpired:
                yield event.plain_result(f"⚠️ 查询服务 {svc} 超时")
                continue
            except Exception as e:
                yield event.plain_result(f"⚠️ 查询服务 {svc} 失败: {e}")
                continue
                
            try:
                # 渲染到 SVG 字符串
                console = Console(record=True, width=80)
                console.print(Text.from_ansi(text))
                svg_data = console.export_svg()
                
                # 添加黑色背景，以便白色文字可见
                if svg_data.startswith('<svg'):
                    svg_data = svg_data.replace('<svg', '<svg style="background-color: black"', 1)
                    
                # 直接从 SVG 内存渲染 PNG
                png_bytes = cairosvg.svg2png(bytestring=svg_data.encode('utf-8'))
                
                # 使用临时文件并确保清理
                import tempfile
                with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as tmp_file:
                    tmp_path = tmp_file.name
                    
                im = Image.open(io.BytesIO(png_bytes))
                im.convert("RGB").save(tmp_path, quality=70)
                
                try:
                    yield event.image_result(tmp_path)
                finally:
                    # 确保临时文件被删除
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                        
            except Exception as e:
                yield event.plain_result(f"⚠️ 生成服务状态图失败: {e}")

    def _create_trend_image(self, times, cpus, mems, sends, recvs) -> bytes:
        buf = io.BytesIO()
        fig = plt.figure(figsize=(8,14))
        gs = fig.add_gridspec(4,1, height_ratios=[0.5,2,2,2])
        ax0 = fig.add_subplot(gs[0,0]); ax0.axis('off'); ax0.text(0,0.5,f"系统运行: {self._get_uptime()}",fontsize=12)
        ax1 = fig.add_subplot(gs[1,0]); ax1.plot(times,cpus); ax1.set_title("CPU %")
        ax2 = fig.add_subplot(gs[2,0], sharex=ax1); ax2.plot(times,mems); ax2.set_title("内存 %")
        ax3 = fig.add_subplot(gs[3,0], sharex=ax1); ax3.plot(times,sends,label="上行"); ax3.plot(times,recvs,label="下行"); ax3.set_title("网络 KB/s"); ax3.legend()
        for ax in (ax1,ax2,ax3):
            ax.xaxis.set_major_locator(mdates.AutoDateLocator()); ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
        fig.tight_layout(); plt.savefig(buf,format='png'); plt.close(fig)
        return buf.getvalue()

    async def terminate(self):
        if self._monitor_task and not self._monitor_task.cancelled(): self._monitor_task.cancel()
        if self._alert_task and not self._alert_task.cancelled(): self._alert_task.cancel()
        await super().terminate()
