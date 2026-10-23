#!/usr/bin/env python3
"""
役男跨縣市代辦體檢「名額」監測程式

監測內政部戶政司「受理外縣市役男申請代辦體檢線上預約系統」
(https://service.dca.moi.gov.tw/military-subPhyExam/...) 的場次頁面，
一旦出現「還有名額」的場次就發通知（macOS 桌面通知 / Telegram / ntfy）。

本程式只「看」場次列表，不會幫你送出申請、也不會填寫任何個資。
看到有名額後，請自己到官網完成申請。

用法：
    python3 monitor.py                 # 依 config.json 持續監測
    python3 monitor.py --once          # 只檢查一次
    python3 monitor.py --once --debug  # 檢查一次並存下 HTML/截圖到 debug/ 方便除錯
    python3 monitor.py --test-notify   # 測試通知是否設定成功
"""

import argparse
import json
import random
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "state.json"
DEBUG_DIR = BASE_DIR / "debug"
ENTRY_URL = "https://service.dca.moi.gov.tw/military-subPhyExam/app/SubPhyExam/phyCountry"
CLOSED_TEXT = "未提供指定服務"  # 系統沒開放時官網顯示「系統訊息：未提供指定服務！」
SYSTEM_URL = "https://service.dca.moi.gov.tw/military-subPhyExam/app/SubPhyExam/phyIndex?areaCode={code}"
# 進入縣市首頁（phyIndex）後，伺服器會記住你選的縣市；「申請作業」頁才是場次與名額
APPLY_URL = "https://service.dca.moi.gov.tw/military-subPhyExam/app/SubPhyExam/phyApply1"

# 各縣市 areaCode（官網網址上的 areaCode 參數）
AREA_CODES = {
    "臺北市": "63000", "新北市": "65000", "桃園市": "68000", "臺中市": "66000",
    "臺南市": "67000", "高雄市": "64000", "基隆市": "10017", "新竹市": "10018",
    "嘉義市": "10020", "新竹縣": "10004", "苗栗縣": "10005", "彰化縣": "10007",
    "南投縣": "10008", "雲林縣": "10009", "嘉義縣": "10010", "屏東縣": "10013",
    "宜蘭縣": "10002", "花蓮縣": "10015", "臺東縣": "10014", "澎湖縣": "10016",
    "金門縣": "09020", "連江縣": "09007",
}

FULL_WORDS = ["額滿", "已滿", "滿額", "已額滿", "截止", "停止受理", "不開放", "已結束", "暫停"]
DATE_RE = re.compile(r"(\d{2,4})\s*[/.\-年]\s*(\d{1,2})\s*[/.\-月]\s*(\d{1,2})")
REMAIN_RE = re.compile(r"(?:剩餘|尚餘|可預約|可報名|剩下)\s*(?:名額|人數)?\s*[:：]?\s*(\d+)")
QUOTA_PAIR_RE = re.compile(r"(\d+)\s*/\s*(\d+)")  # 例如「已報名/名額」 12/30


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def load_config(path):
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    cfg.setdefault("interval_seconds", 300)
    cfg["interval_seconds"] = max(60, int(cfg["interval_seconds"]))  # 至少 60 秒，避免造成官網負擔
    cfg.setdefault("household_county", "")
    cfg.setdefault("hospital_keywords", [])
    cfg.setdefault("date_from", "")
    cfg.setdefault("date_to", "")
    cfg.setdefault("household_town", "")
    cfg.setdefault("headless", True)
    cfg.setdefault("notify", {})
    if cfg["household_county"] not in AREA_CODES:
        sys.exit(f"config 的 household_county（戶籍地）要填縣市全名，例如「臺南市」，可選：{', '.join(AREA_CODES)}")
    if cfg.get("url"):
        cfg["targets"] = [("自訂網址", cfg["url"])]
        return cfg
    want = cfg.get("exam_county") or "全部"
    if want in ("全部", "all", "ALL"):
        # 不能在戶籍地縣市申請代檢，所以排除戶籍地
        want = [c for c in AREA_CODES if c != cfg["household_county"]]
    elif isinstance(want, str):
        want = [want]
    bad = [c for c in want if c not in AREA_CODES]
    if bad:
        sys.exit(f"config 的 exam_county 有不認得的縣市：{bad}，可選：「全部」或 {', '.join(AREA_CODES)}")
    cfg["targets"] = [(c, SYSTEM_URL.format(code=AREA_CODES[c])) for c in want]
    return cfg


# ---------------------------------------------------------------- 解析 ----

def parse_date(text):
    m = DATE_RE.search(text)
    if not m:
        return None
    y, mo, d = (int(x) for x in m.groups())
    if y < 1911:  # 民國年
        y += 1911
    try:
        return datetime(y, mo, d).date()
    except ValueError:
        return None


def to_date(s):
    return parse_date(s) if s else None


def classify_row(text):
    """回傳 (是否有名額, 說明)。判斷不出來時回傳 (None, 原因)。"""
    text = DATE_RE.sub(" ", text)  # 先去掉日期，避免 115/10 被當成 已報名/名額
    if any(w in text for w in FULL_WORDS):
        return False, "額滿/截止"
    m = REMAIN_RE.search(text)
    if m:
        n = int(m.group(1))
        return n > 0, f"剩餘 {n}"
    m = QUOTA_PAIR_RE.search(text)
    if m:
        used, total = int(m.group(1)), int(m.group(2))
        if total >= used:
            return total > used, f"{used}/{total}"
    if re.search(r"(預約|報名|申請|選擇)", text):
        return True, "可選擇"
    return None, "無法判斷"


def extract_phy_table(page):
    """讀官網第3步的梯次表 #myTable：醫院名稱 / 體檢時間 / 截止時間 / 開放名額 / 剩餘名額。
    表格有分頁，用 DataTables 的 fnGetNodes() 取得所有列（不只第一頁）。"""
    rows = page.evaluate(
        """() => {
            const t = document.querySelector('#myTable');
            if (!t) return null;
            let trs = [];
            try { trs = window.myTable && window.myTable.fnGetNodes ? Array.from(window.myTable.fnGetNodes()) : []; } catch (e) {}
            if (!trs.length) trs = Array.from(t.querySelectorAll('tbody tr'));
            return trs.map(tr => {
                const r = tr.querySelector('input[type=radio]');
                return {id: r ? r.value : '', disabled: r ? r.disabled : true,
                        cells: Array.from(tr.querySelectorAll('td')).map(td => td.innerText.trim())};
            });
        }"""
    )
    if rows is None:
        return None
    now = datetime.now()
    sessions = []
    for r in rows:
        c = r["cells"]
        if len(c) < 6:  # 例如「表中無資料」
            continue
        hospital, exam, deadline, total, remain = c[1], c[2], c[3], c[4], c[5]
        d = parse_date(exam)
        if not d:
            continue
        remain_n = int(re.sub(r"\D", "", remain) or 0)
        dl = parse_date(deadline)
        expired = False
        if dl:
            m = re.search(r"(\d{1,2}):(\d{2})", deadline)
            hh, mm = (int(m.group(1)), int(m.group(2))) if m else (23, 59)
            expired = datetime(dl.year, dl.month, dl.day, hh, mm) < now
        ok = remain_n > 0 and not expired and not r["disabled"]
        why = "已過截止時間" if expired else f"剩餘 {remain_n}/{total}"
        sessions.append({
            "date": d.isoformat(), "key": r["id"] or f"{d}{hospital}",
            "text": f"{hospital} {exam}（截止 {deadline}）", "available": ok,
            "reason": why, "remain": remain_n,
        })
    return sessions


def extract_sessions(page):
    """從頁面上所有表格列 / 選項中找出含日期的場次。"""
    rows = page.evaluate(
        """() => {
            const out = [];
            const root = document.querySelector('.col-md-9') || document;
            const vis = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
            root.querySelectorAll('tr').forEach(tr => {
                if (tr.querySelector('th') && !tr.querySelector('td')) return;
                const btn = tr.querySelector('input[type=radio], input[type=checkbox], button, a');
                const dis = btn ? (btn.disabled || btn.getAttribute('aria-disabled') === 'true') : null;
                out.push({text: tr.innerText.replace(/\\s+/g, ' ').trim(), disabled: dis, visible: vis(tr)});
            });
            root.querySelectorAll('option').forEach(o => {
                out.push({text: o.textContent.replace(/\\s+/g, ' ').trim(), disabled: o.disabled, visible: true});
            });
            root.querySelectorAll('li, .card, .session, .list-group-item').forEach(el => {
                if (el.querySelector('tr')) return;
                out.push({text: el.innerText.replace(/\\s+/g, ' ').trim(), disabled: null, visible: vis(el)});
            });
            return out;
        }"""
    )
    sessions, seen = [], set()
    for r in rows:
        t = r["text"]
        if not t or len(t) > 300 or t in seen or not r["visible"]:
            continue
        d = parse_date(t)
        if not d:
            continue
        seen.add(t)
        ok, why = classify_row(t)
        if r["disabled"] is True:
            ok, why = False, "選項不可選"
        # 用「日期＋去掉數字的文字」當識別，剩餘名額數字變動時不會重複通知
        key = d.isoformat() + re.sub(r"\d+", "", DATE_RE.sub("", t))
        sessions.append({"date": d.isoformat(), "key": key, "text": t, "available": ok, "reason": why})
    return sessions


def wanted(s, cfg):
    d = datetime.fromisoformat(s["date"]).date()
    lo, hi = to_date(cfg["date_from"]), to_date(cfg["date_to"])
    if lo and d < lo:
        return False
    if hi and d > hi:
        return False
    kws = cfg["hospital_keywords"]
    return not kws or any(k in s["text"] for k in kws)


# ---------------------------------------------------------- 瀏覽器流程 ----

def check_county(browser, cfg, county, url, debug=False):
    """依官網流程走到「第3步：選擇體檢梯次」並讀出場次。
    流程：縣市首頁(phyIndex) → 第1步須知(phyApply1) → 第2步選戶籍地(phyApply2) → 第3步梯次(phyApply3)。
    只會選戶籍地，不會填任何個資，也不會進到第4步。回傳 None 代表該縣市未開放。"""
    page = browser.new_page(locale="zh-TW")
    alerts = []
    page.on("dialog", lambda d: (alerts.append(d.message), d.accept()))

    def snap(tag):
        if debug:
            save_debug(page, f"{county}_{tag}")

    try:
        page.goto(url, wait_until="networkidle", timeout=45000)
        if CLOSED_TEXT in page.inner_text("body"):
            snap("未開放")
            return None
        # 第1步：申請須知
        page.goto(APPLY_URL, wait_until="networkidle", timeout=45000)
        snap("1須知")
        btn = page.locator("#validateData1")
        if not btn.count():
            log(f"[{county}] 申請作業頁沒有「我已閱讀」按鈕：{main_text(page)[:80]}")
            return []
        btn.click()
        page.wait_for_load_state("networkidle", timeout=30000)
        # 第2步：選戶籍地（縣市＋鄉鎮），鄉鎮清單是點選縣市後才載入
        snap("2戶籍地")
        if not page.locator("#siteAreaCode").count():
            log(f"[{county}] 沒有出現選擇戶籍地的畫面：{main_text(page)[:80]} {' '.join(alerts)}")
            return []
        page.select_option("#siteAreaCode", AREA_CODES[cfg["household_county"]])
        page.wait_for_function("document.querySelectorAll('#townCode option').length > 1", timeout=15000)
        towns = page.eval_on_selector_all("#townCode option", "os => os.map(o => [o.value, o.textContent.trim()])")
        towns = [t for t in towns if t[0]]
        pick = next((t for t in towns if cfg.get("household_town") and cfg["household_town"] in t[1]), towns[0])
        page.select_option("#townCode", pick[0])
        page.wait_for_timeout(800)  # 等郵遞區號自動帶入
        with page.expect_navigation(wait_until="networkidle", timeout=30000):
            page.click("#validateData")
        # 第3步：選擇體檢梯次
        snap("3梯次")
        sessions = extract_phy_table(page)
        if sessions is None:  # 頁面結構跟預期不同，退回通用解析
            sessions = extract_sessions(page)
        if not sessions:
            msg = " ".join(alerts) or main_text(page)[:120]
            log(f"[{county}] 第3步沒有可辨識的梯次：{msg}")
    finally:
        page.close()
    for s in sessions:
        s["county"], s["url"], s["key"] = county, url, county + s["key"]
    return sessions


def main_text(page):
    """主內容區（不含左側選單）的文字。"""
    loc = page.locator(".col-md-9")
    txt = loc.first.inner_text() if loc.count() else page.inner_text("body")
    return re.sub(r"\s+", " ", txt).strip()


def system_is_open(browser, debug=False):
    page = browser.new_page(locale="zh-TW")
    try:
        page.goto(ENTRY_URL, wait_until="networkidle", timeout=45000)
        if debug:
            save_debug(page, "總入口")
        return CLOSED_TEXT not in page.inner_text("body")
    finally:
        page.close()


def check_once(cfg, debug=False):
    """先看總入口有沒有開放，有開放才逐一檢查縣市。
    回傳 (系統是否開放, 所有場次, 有開放的縣市)。"""
    from playwright.sync_api import sync_playwright

    all_sessions, failed, opened = [], [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=cfg["headless"])
        if not cfg.get("url") and not system_is_open(browser, debug):
            browser.close()
            return False, [], []
        for i, (county, url) in enumerate(cfg["targets"]):
            if i:
                time.sleep(random.uniform(3, 6))  # 縣市之間稍微停一下，避免短時間大量連線
            try:
                found = check_county(browser, cfg, county, url, debug)
                if found is None:
                    continue
                if found:
                    opened.append((county, url))  # 有列出體檢梯次才算「開放中」
                all_sessions += found
                if not found:
                    log(f"[{county}] 沒抓到場次（可能目前沒開放，或頁面結構不同）")
            except Exception as e:
                failed.append(county)
                log(f"[{county}] 檢查失敗：{e}")
        browser.close()
    if failed and len(failed) == len(cfg["targets"]):
        raise RuntimeError(f"所有縣市都連線失敗（{', '.join(failed)}）")
    return True, all_sessions, opened


def save_debug(page, tag):
    DEBUG_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%H%M%S")
    (DEBUG_DIR / f"{stamp}_{tag}.html").write_text(page.content(), encoding="utf-8")
    page.screenshot(path=str(DEBUG_DIR / f"{stamp}_{tag}.png"), full_page=True)


# ---------------------------------------------------------------- 通知 ----

def notify(cfg, title, body):
    n = cfg["notify"]
    log(f"通知：{title}\n{body}")
    print("\a", end="", flush=True)  # 終端機嗶一聲
    if n.get("desktop", True) and sys.platform == "darwin":
        script = f'display notification {json.dumps(body[:200])} with title {json.dumps(title)} sound name "Glass"'
        subprocess.run(["osascript", "-e", script], check=False)
    elif n.get("desktop", True) and sys.platform.startswith("linux"):
        subprocess.run(["notify-send", title, body], check=False)
    tg = n.get("telegram") or {}
    if tg.get("bot_token") and tg.get("chat_id"):
        data = urllib.parse.urlencode({"chat_id": tg["chat_id"], "text": f"{title}\n{body}"}).encode()
        _post(f"https://api.telegram.org/bot{tg['bot_token']}/sendMessage", data)
    if n.get("ntfy_topic"):
        payload = {"topic": n["ntfy_topic"], "title": title, "message": body, "priority": 4}
        req = urllib.request.Request("https://ntfy.sh", data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        _post(req)


def _post(url_or_req, data=None):
    try:
        urllib.request.urlopen(url_or_req, data=data, timeout=15).read()
    except Exception as e:
        log(f"通知送出失敗：{e}")


# ---------------------------------------------------------------- 主程式 ----

def run(cfg, once=False, debug=False):
    state = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    notified = set(state.get("notified", []))
    was_open = state.get("open_counties", [])
    fails = 0
    names = "、".join(c for c, _ in cfg["targets"])
    log(f"開始監測 {len(cfg['targets'])} 個縣市：{names}（每輪間隔 {cfg['interval_seconds']} 秒）")
    while True:
        try:
            is_open, sessions, opened = check_once(cfg, debug=debug)
            fails = 0
            if not is_open:
                log("官網系統目前沒開放（顯示「未提供指定服務」），等開放時會通知你")
            elif not opened:
                log("目前你選的縣市都沒有可預約的體檢梯次")
            else:
                log(f"有列出體檢梯次的縣市：{'、'.join(c for c, _ in opened)}")
            newly = [(c, u) for c, u in opened if c not in was_open]
            if newly:
                body = "\n".join(f"【{c}】{u}" for c, u in newly)
                notify(cfg, "役男代檢：有縣市開放梯次了！", body)
            was_open = [c for c, _ in opened]
            mine = [s for s in sessions if wanted(s, cfg)]
            avail = [s for s in mine if s["available"]]
            unknown = [s for s in mine if s["available"] is None]
            if is_open:
                log(f"找到 {len(sessions)} 個場次，符合條件 {len(mine)} 個，有名額 {len(avail)} 個")
            county = None
            for s in mine:
                if s["county"] != county:
                    county = s["county"]
                    print(f"  【{county}】")
                mark = {True: "✅", False: "❌", None: "❔"}[s["available"]]
                print(f"   {mark} {s['text']}  ({s['reason']})")
            if unknown and debug:
                log("有場次無法判斷是否有名額，請跟我說，我會看 debug/ 內「3梯次」的截圖調整規則")
            new = [s for s in avail if s["key"] not in notified]
            if new:
                lines = []
                for c in dict.fromkeys(s["county"] for s in new):
                    lines.append(f"【{c}】{next(s['url'] for s in new if s['county'] == c)}")
                    lines += [f"• {s['text']} {s['reason']}" for s in new if s["county"] == c]
                body = "\n".join(lines)
                notify(cfg, "役男代檢有名額了！", body)
                notified.update(s["key"] for s in new)
            # 名額消失的場次從已通知清單移除，之後再釋出時會再通知一次
            notified &= {s["key"] for s in avail}
            STATE_FILE.write_text(json.dumps({"notified": sorted(notified), "open_counties": was_open}, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as e:
            fails += 1
            log(f"檢查失敗（第 {fails} 次）：{e}")
            if fails == 5:
                notify(cfg, "役男代檢監測：連續失敗", f"已連續失敗 5 次，最後錯誤：{e}")
        if once:
            return
        wait = cfg["interval_seconds"] * (1 + min(fails, 4)) + random.uniform(0, 30)
        time.sleep(wait)


def main():
    ap = argparse.ArgumentParser(description="役男跨縣市代辦體檢名額監測")
    ap.add_argument("-c", "--config", default=str(BASE_DIR / "config.json"))
    ap.add_argument("--once", action="store_true", help="只檢查一次")
    ap.add_argument("--debug", action="store_true", help="存下每一步的 HTML 和截圖到 debug/")
    ap.add_argument("--county", help="這次只檢查指定縣市，例如 --county 臺北市")
    ap.add_argument("--show", action="store_true", help="顯示瀏覽器視窗（非無頭模式）")
    ap.add_argument("--test-notify", action="store_true", help="送一則測試通知")
    a = ap.parse_args()
    if not Path(a.config).exists():
        sys.exit(f"找不到 {a.config}，請先複製 config.example.json 成 config.json 並修改")
    cfg = load_config(a.config)
    if a.county:
        if a.county not in AREA_CODES:
            sys.exit(f"不認得的縣市：{a.county}")
        cfg["targets"] = [(a.county, SYSTEM_URL.format(code=AREA_CODES[a.county]))]
    if a.show:
        cfg["headless"] = False
    if a.test_notify:
        notify(cfg, "役男代檢監測：測試通知", "看到這則訊息代表通知設定成功 🎉")
        return
    try:
        run(cfg, once=a.once, debug=a.debug)
    except KeyboardInterrupt:
        log("已停止")


if __name__ == "__main__":
    main()
