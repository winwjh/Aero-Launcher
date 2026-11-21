"""UPnP IGD 端口映射（纯标准库实现，不依赖第三方包）。

流程：SSDP 发现网关 -> 拉取设备描述 XML -> 找到 WANIPConnection 控制地址
-> SOAP 调 AddPortMapping / GetExternalIPAddress / DeletePortMapping。

只在家庭路由器开启 UPnP 且宽带本身有公网 IP 时才能真正让外网访问；
成功时返回外部 IP，失败时抛 RuntimeError（调用方负责转成友好提示）。
"""
import ipaddress
import re
import socket
import urllib.parse
import urllib.request

SSDP_ADDR = ("239.255.255.250", 1900)
SSDP_ST = "urn:schemas-upnp-org:device:InternetGatewayDevice:1"
TIMEOUT = 4


def _local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def _ssdp_discover():
    """M-SEARCH 找出内网里的 IGD 设备，返回其 description XML 的 URL 列表"""
    msg = ("\r\n".join([
        "M-SEARCH * HTTP/1.1",
        f"HOST: {SSDP_ADDR[0]}:{SSDP_ADDR[1]}",
        f"ST: {SSDP_ST}",
        "MAN: \"ssdp:discover\"",
        "MX: 2",
        "", "",
    ])).encode("ascii")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.settimeout(TIMEOUT)
    try:
        sock.sendto(msg, SSDP_ADDR)
        urls = []
        deadline = sock.gettimeout()
        import time
        end = time.time() + deadline
        while time.time() < end:
            try:
                data, _ = sock.recvfrom(4096)
                text = data.decode("utf-8", "ignore")
                m = re.search(r"(?i)location:\s*(\S+)", text)
                if m and m.group(1) not in urls:
                    urls.append(m.group(1))
            except socket.timeout:
                break
        return urls
    finally:
        sock.close()


def _fetch(url, timeout=TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": "AeroLauncher/3.4"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def _soap(control_url, service_type, action, args_xml):
    body = f"""<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"
            s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>
    <u:{action} xmlns:u="{service_type}">{args_xml}</u:{action}>
  </s:Body>
</s:Envelope>"""
    req = urllib.request.Request(
        control_url, data=body.encode("utf-8"), method="POST",
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"{service_type}#{action}"',
        })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")
        m = re.search(r"<errorCode>(\d+)</errorCode>", detail)
        raise RuntimeError(f"UPnP:{action}:{m.group(1) if m else e.code}")


def _find_wan_service(xml):
    """从设备描述里找 WANIPConnection / WANPPPConnection 的控制地址与服务类型"""
    # 抓所有 service 块
    services = re.findall(r"<service>(.*?)</service>", xml, re.S)
    prefer = []
    for s in services:
        st = re.search(r"<serviceType>\s*([^<]+)", s)
        cu = re.search(r"<controlURL>\s*([^<]+)", s)
        if not st or not cu:
            continue
        st_t, cu_t = st.group(1).strip(), cu.group(1).strip()
        if "WANIPConnection" in st_t or "WANPPPConnection" in st_t:
            prefer.append((st_t, cu_t))
    # WANIPConnection 优先（PPP 也能加映射，但 IP 接口不同）
    prefer.sort(key=lambda x: 0 if "WANIPConnection" in x[0] else 1)
    return prefer


def _abs_url(base, path):
    if path.startswith("http"):
        return path
    return urllib.parse.urljoin(base, path)


def map_port(port, proto="TCP", lease=0, description="Aero Launcher"):
    """把外网 port 映射到本机同端口。成功返回外部 IP 字符串。"""
    local_ip = _local_ip()
    if ipaddress.ip_address(local_ip).is_private is False:
        # 本机就有公网地址（罕见），无需映射
        return local_ip

    errors = []
    for desc_url in _ssdp_discover():
        try:
            xml = _fetch(desc_url)
        except Exception as e:
            errors.append(f"desc:{e}")
            continue
        for st, cu in _find_wan_service(xml):
            ctrl = _abs_url(desc_url, cu)
            args = (f"<NewRemoteHost></NewRemoteHost>"
                    f"<NewExternalPort>{port}</NewExternalPort>"
                    f"<NewProtocol>{proto}</NewProtocol>"
                    f"<NewInternalPort>{port}</NewInternalPort>"
                    f"<NewInternalClient>{local_ip}</NewInternalClient>"
                    f"<NewEnabled>1</NewEnabled>"
                    f"<NewPortMappingDescription>{description}</NewPortMappingDescription>"
                    f"<NewLeaseDuration>{lease}</NewLeaseDuration>")
            try:
                _soap(ctrl, st, "AddPortMapping", args)
                # 拿外部 IP 给用户显示
                try:
                    resp = _soap(ctrl, st, "GetExternalIPAddress", "")
                    m = re.search(r"<NewExternalIPAddress>\s*([\d.]+)", resp)
                    ext_ip = m.group(1) if m else local_ip
                except Exception:
                    ext_ip = local_ip
                return ext_ip
            except RuntimeError as e:
                # 718 = 冲突（可能已映射过），当作成功
                if "718" in str(e) or "ConflictInMappingEntry" in str(e):
                    try:
                        resp = _soap(ctrl, st, "GetExternalIPAddress", "")
                        m = re.search(r"<NewExternalIPAddress>\s*([\d.]+)", resp)
                        return m.group(1) if m else local_ip
                    except Exception:
                        return local_ip
                errors.append(str(e))
    if errors:
        raise RuntimeError("UPnP失败: " + "; ".join(errors[:3]))
    raise RuntimeError("未发现支持UPnP的路由器")


def unmap_port(port, proto="TCP"):
    for desc_url in _ssdp_discover():
        try:
            xml = _fetch(desc_url)
        except Exception:
            continue
        for st, cu in _find_wan_service(xml):
            ctrl = _abs_url(desc_url, cu)
            args = (f"<NewRemoteHost></NewRemoteHost>"
                    f"<NewExternalPort>{port}</NewExternalPort>"
                    f"<NewProtocol>{proto}</NewProtocol>")
            try:
                _soap(ctrl, st, "DeletePortMapping", args)
                return True
            except RuntimeError:
                continue
    return False


def external_ip():
    """优先从 IGD 拿公网 IP，拿不到退回 STUN/baidu"""
    for desc_url in _ssdp_discover():
        try:
            xml = _fetch(desc_url)
            for st, cu in _find_wan_service(xml):
                resp = _soap(_abs_url(desc_url, cu), st, "GetExternalIPAddress", "")
                m = re.search(r"<NewExternalIPAddress>\s*([\d.]+)", resp)
                if m:
                    ip = m.group(1)
                    if not ipaddress.ip_address(ip).is_private:
                        return ip
        except Exception:
            continue
    try:
        req = urllib.request.Request("https://api.ipify.org", timeout=5)
        return urllib.request.urlopen(req, timeout=5).read().decode().strip()
    except Exception:
        return ""


if __name__ == "__main__":
    print("local:", _local_ip())
    print("ext:", external_ip())
