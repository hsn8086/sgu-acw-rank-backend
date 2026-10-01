"""读取各平台公开资料，用于绑定时的签名校验与后续抓取。

四个平台的差异（均已实测）：

============  ==================  ====================  ==============
平台           资料字段             读取方式               需要登录
============  ==================  ====================  ==============
Codeforces    organization        官方 API              否
AtCoder       Affiliation         用户页 HTML           否
洛谷           slogan              用户页内嵌 JSON        否（需反爬 cookie）
牛客           coder-brief         用户页 HTML           否
============  ==================  ====================  ==============

洛谷那个「反爬 cookie」不是登录凭据：它只需要一个随机的 ``__client_id``，
服务端用它做频次统计。第一次请求会返回 302 并下发 ``C3VK``，带上它重试即可，
所以这里统一做一次重试。

解题记录的可得性并不一致，且与资料字段无关：

* CF、AtCoder、牛客都带精确时间戳，能直接算出每天过了几题。
* 洛谷只给「累计通过哪些题」的集合，没有任何时间信息，只能靠快照差分。

牛客容易被误判成拿不到：它的**比赛**记录（rating-history）确实没有逐题
数据，但**练习**记录（practice-coding）是公开且完整的，服务端直出表格，
带提交时间和判题结果。别把前者当成全部。
"""

from __future__ import annotations

import html
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from .models import Platform

#: 统计口径按东八区。牛客页面上的时间没有时区标记，本身就是北京时间。
CST = timezone(timedelta(hours=8))

#: 浏览器 UA。几个平台都会拒绝空 UA 或明显的脚本 UA。
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

DEFAULT_TIMEOUT = httpx.Timeout(15.0, connect=8.0)


class FetchError(Exception):
    """读不到资料。``not_found`` 用来区分「账号不存在」和「网络/解析问题」。"""

    def __init__(self, message: str, *, not_found: bool = False) -> None:
        super().__init__(message)
        self.not_found = not_found


@dataclass(slots=True)
class Profile:
    """从平台读到的一份资料。"""

    platform: str
    identifier: str
    #: 用于校验签名的文本。平台没有这个字段时为 None。
    signature: str | None = None
    display_name: str | None = None
    rating: int | None = None
    max_rating: int | None = None
    #: 平台内部的数字 id（洛谷/牛客用得上），拿不到则为 None。
    internal_id: str | None = None


def _clean(text: str) -> str:
    """去掉标签、反转义实体、压平空白。"""
    without_tags = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", html.unescape(without_tags)).strip()


# --------------------------------------------------------------------------- #
# Codeforces
# --------------------------------------------------------------------------- #


def fetch_codeforces(client: httpx.Client, handle: str) -> Profile:
    response = client.get(
        "https://codeforces.com/api/user.info",
        params={"handles": handle},
    )
    if response.status_code != 200:
        raise FetchError(f"Codeforces 返回 {response.status_code}")
    payload = response.json()
    if payload.get("status") != "OK":
        comment = payload.get("comment", "")
        # 官方对不存在的 handle 回 FAILED，comment 里带 "not found"。
        not_found = "not found" in comment.lower()
        raise FetchError(f"Codeforces: {comment or '查询失败'}", not_found=not_found)

    user = payload["result"][0]
    return Profile(
        platform=Platform.CODEFORCES,
        identifier=user["handle"],
        signature=user.get("organization") or "",
        display_name=user.get("handle"),
        rating=user.get("rating"),
        max_rating=user.get("maxRating"),
    )


# --------------------------------------------------------------------------- #
# AtCoder
# --------------------------------------------------------------------------- #


def fetch_atcoder(client: httpx.Client, handle: str) -> Profile:
    response = client.get(f"https://atcoder.jp/users/{handle}")
    if response.status_code == 404:
        raise FetchError("AtCoder 上没有这个用户", not_found=True)
    if response.status_code != 200:
        raise FetchError(f"AtCoder 返回 {response.status_code}")

    page = response.text
    # 资料表是 <th class="no-break">标题</th><td ...>值</td> 的形式。
    affiliation = re.search(r"<th[^>]*>\s*Affiliation\s*</th>\s*<td[^>]*>(.*?)</td>", page, re.S)
    rating = re.search(r"<th[^>]*>\s*Rating\s*</th>\s*<td[^>]*>(.*?)</td>", page, re.S)
    highest = re.search(r"<th[^>]*>\s*Highest Rating\s*</th>\s*<td[^>]*>(.*?)</td>", page, re.S)

    def number(match: re.Match[str] | None) -> int | None:
        if not match:
            return None
        digits = re.search(r"\d+", _clean(match.group(1)).replace(",", ""))
        return int(digits.group()) if digits else None

    return Profile(
        platform=Platform.ATCODER,
        identifier=handle,
        signature=_clean(affiliation.group(1)) if affiliation else "",
        display_name=handle,
        rating=number(rating),
        max_rating=number(highest),
    )


# --------------------------------------------------------------------------- #
# 洛谷
# --------------------------------------------------------------------------- #


def _luogu_get(client: httpx.Client, url: str) -> str:
    """带反爬握手的一次 GET。

    洛谷会先回 302 并下发 C3VK，带上它再请求才拿到内容。这里随机生成一个
    __client_id：官方把频次限制绑在这个值上，随机化可以避免多个成员共用
    同一个额度，也不需要任何真实账号。
    """
    cookie = {"__client_id": secrets.token_hex(20)}
    first = client.get(url, cookies=cookie)
    if first.status_code == 200:
        return first.text

    # 把服务端下发的 cookie 合并进来重试一次。
    merged = dict(cookie)
    merged.update({k: v for k, v in first.cookies.items()})
    second = client.get(url, cookies=merged)
    if second.status_code == 404:
        raise FetchError("洛谷上没有这个用户", not_found=True)
    if second.status_code != 200:
        raise FetchError(f"洛谷返回 {second.status_code}")
    return second.text


def _luogu_context(page: str) -> dict:
    """取出用户页内嵌的 lentille-context JSON。"""
    import json

    match = re.search(
        r'<script id="lentille-context" type="application/json">(.*?)</script>',
        page,
        re.S,
    )
    if not match:
        raise FetchError("洛谷页面结构变了，取不到数据")
    try:
        return json.loads(html.unescape(match.group(1)))
    except json.JSONDecodeError as exc:
        raise FetchError("洛谷返回的数据无法解析") from exc


def resolve_luogu_uid(client: httpx.Client, name: str) -> tuple[str, str] | None:
    """按用户名搜出 (uid, name)。搜不到返回 None。"""
    page = _luogu_get(client, f"https://www.luogu.com.cn/user/search?keyword={name}")
    import json

    try:
        payload = json.loads(page)
    except json.JSONDecodeError:
        return None
    for user in payload.get("users", []):
        if user.get("name", "").lower() == name.lower():
            return str(user["uid"]), user["name"]
    return None


def fetch_luogu(client: httpx.Client, identifier: str) -> Profile:
    """``identifier`` 可以是数字 uid，也可以是用户名。"""
    if not identifier.isdigit():
        found = resolve_luogu_uid(client, identifier)
        if found is None:
            raise FetchError("洛谷上没有这个用户", not_found=True)
        identifier = found[0]

    page = _luogu_get(client, f"https://www.luogu.com.cn/user/{identifier}")
    context = _luogu_context(page)
    data = context.get("data") or {}
    user = data.get("user") or {}
    if not user:
        raise FetchError("洛谷上没有这个用户", not_found=True)

    # 洛谷用户页本身不带 rating，等级分在 elo 里。个人中心页才有完整数据，
    # 但它只在登录后可读，所以这里退一步：拿不到就算了，不影响榜单排序
    # （没 rating 的排最后）。
    rating = None
    elo = data.get("elo")
    if isinstance(elo, list) and elo:
        latest = elo[-1]
        if isinstance(latest, dict):
            value = latest.get("rating")
            rating = int(value) if isinstance(value, (int, float)) else None

    return Profile(
        platform=Platform.LUOGU,
        identifier=str(user.get("uid") or identifier),
        # 洛谷把个人简介放在 slogan（签名）和 introduction（简介）两处，
        # 签名是主页上显示的那一行，验证时两处都认。
        signature="\n".join(filter(None, [user.get("slogan"), user.get("introduction")])),
        display_name=user.get("name"),
        rating=rating,
        internal_id=str(user.get("uid") or identifier),
    )


# --------------------------------------------------------------------------- #
# 牛客
# --------------------------------------------------------------------------- #


def fetch_nowcoder(client: httpx.Client, identifier: str) -> Profile:
    """``identifier`` 是牛客的 uid。"""
    if not identifier.isdigit():
        raise FetchError("牛客需要填数字 uid")

    response = client.get(f"https://ac.nowcoder.com/acm/contest/profile/{identifier}")
    if response.status_code != 200:
        raise FetchError(f"牛客返回 {response.status_code}")

    page = response.text
    brief = re.search(r'<div class="coder-brief">(.*?)</div>', page, re.S)
    if brief is None:
        # 页面能打开但没有这个区块，说明 uid 不存在。
        raise FetchError("牛客上没有这个用户", not_found=True)

    rating = None
    rating_match = re.search(r"(\d+)\s*Rating", _clean(page))
    if rating_match:
        rating = int(rating_match.group(1))

    return Profile(
        platform=Platform.NOWCODER,
        identifier=identifier,
        signature=_clean(brief.group(1)),
        display_name=identifier,
        rating=rating,
        internal_id=identifier,
    )


# --------------------------------------------------------------------------- #
# 统一入口
# --------------------------------------------------------------------------- #

_FETCHERS = {
    Platform.CODEFORCES: fetch_codeforces,
    Platform.ATCODER: fetch_atcoder,
    Platform.LUOGU: fetch_luogu,
    Platform.NOWCODER: fetch_nowcoder,
}


def fetch_profile(platform: str, identifier: str) -> Profile:
    """读一个平台的资料。异常统一是 :class:`FetchError`。"""
    fetcher = _FETCHERS.get(platform)
    if fetcher is None:
        raise FetchError(f"不支持的平台：{platform}")

    headers = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    try:
        with httpx.Client(
            headers=headers, timeout=DEFAULT_TIMEOUT, follow_redirects=False
        ) as client:
            return fetcher(client, identifier)
    except httpx.TimeoutException as exc:
        raise FetchError("请求超时，稍后重试") from exc
    except httpx.HTTPError as exc:
        raise FetchError(f"网络错误：{exc}") from exc


def check_signature(platform: str, identifier: str, token: str) -> tuple[bool, str]:
    """校验 ``token`` 是否出现在该账号的签名里。

    返回 ``(是否通过, 说明)``。用「包含」而不是「相等」：成员的签名里往往
    本来就有别的内容，不该要求他们清空。
    """
    try:
        profile = fetch_profile(platform, identifier)
    except FetchError as exc:
        return False, str(exc)

    signature = profile.signature or ""
    if not signature:
        label = Platform.LABELS.get(platform, platform)
        return False, f"{label} 上没有找到签名字段，请先填写再验证"

    # 大小写不敏感：成员可能把 SGU-xxxx 抄成小写。
    if token.lower() in signature.lower():
        return True, "验证通过"
    return False, "签名里没有找到验证码，请确认已保存"


# --------------------------------------------------------------------------- #
# 已过题目集合（用于每日练习量统计）
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class SolvedSet:
    """一个账号做过的题目。

    ``problems`` 是 题目标识 → 首次通过时间（epoch 秒）。时间未知时为 0，
    洛谷就属于这种：它只给出累计通过的题目集合，没有任何时间信息。
    """

    platform: str
    identifier: str
    problems: dict[str, int]


def fetch_solved_codeforces(client: httpx.Client, handle: str) -> SolvedSet:
    """拉全量提交，取每题最早的 AC 时间。"""
    response = client.get(
        "https://codeforces.com/api/user.status",
        params={"handle": handle, "from": 1, "count": 100000},
    )
    if response.status_code != 200:
        raise FetchError(f"Codeforces 返回 {response.status_code}")
    payload = response.json()
    if payload.get("status") != "OK":
        comment = payload.get("comment", "")
        raise FetchError(
            f"Codeforces: {comment or '查询失败'}",
            not_found="not found" in comment.lower(),
        )

    problems: dict[str, int] = {}
    for submission in payload["result"]:
        if submission.get("verdict") != "OK":
            continue
        problem = submission.get("problem") or {}
        contest_id = problem.get("contestId")
        index = problem.get("index")
        if contest_id is None or index is None:
            # 一些题（如 acmsguru）没有 contestId，用名字兜底。
            key = f"name:{problem.get('name', '')}"
        else:
            key = f"{contest_id}{index}"
        created = int(submission.get("creationTimeSeconds") or 0)
        # 同一题可能多次 AC，只记最早那次。
        if key not in problems or (created and created < problems[key]):
            problems[key] = created

    return SolvedSet(platform=Platform.CODEFORCES, identifier=handle, problems=problems)


def fetch_solved_atcoder(client: httpx.Client, handle: str) -> SolvedSet:
    """从 kenkoooo 的镜像拉提交，取每题最早的 AC 时间。"""
    response = client.get(
        "https://kenkoooo.com/atcoder/atcoder-api/v3/user/submissions",
        params={"user": handle, "from_second": 0},
    )
    if response.status_code != 200:
        raise FetchError(f"AtCoder 数据源返回 {response.status_code}")
    submissions = response.json()
    if not isinstance(submissions, list):
        raise FetchError("AtCoder 数据源返回了意外的内容")

    problems: dict[str, int] = {}
    for submission in submissions:
        if submission.get("result") != "AC":
            continue
        problem_id = submission.get("problem_id")
        if not problem_id:
            continue
        epoch = int(submission.get("epoch_second") or 0)
        if problem_id not in problems or (epoch and epoch < problems[problem_id]):
            problems[problem_id] = epoch

    return SolvedSet(platform=Platform.ATCODER, identifier=handle, problems=problems)


def fetch_solved_luogu(client: httpx.Client, identifier: str) -> SolvedSet:
    """洛谷只给累计通过集合，时间一律为 0，靠快照差分出每日增量。"""
    if not identifier.isdigit():
        found = resolve_luogu_uid(client, identifier)
        if found is None:
            raise FetchError("洛谷上没有这个用户", not_found=True)
        identifier = found[0]

    page = _luogu_get(client, f"https://www.luogu.com.cn/user/{identifier}/practice")
    data = (_luogu_context(page).get("data")) or {}
    passed = data.get("passed")
    if passed is None:
        # privacy=True 的用户会返回空列表，这与「一题没做」无法区分。
        raise FetchError("该用户的练习记录未公开，无法统计")
    if not isinstance(passed, list):
        raise FetchError("洛谷返回了意外的练习数据")

    problems = {str(item.get("pid")): 0 for item in passed if item.get("pid")}
    return SolvedSet(platform=Platform.LUOGU, identifier=identifier, problems=problems)


def fetch_solved_nowcoder(client: httpx.Client, identifier: str) -> SolvedSet:
    """牛客的练习记录。``identifier`` 是数字 uid。

    页面服务端直出表格，每行一次提交，带精确时间与判题结果，所以属于
    「有时间戳」那一类，不需要快照差分。一页最多 100 条，翻到没有新行为止。

    注意区分两件事：牛客的**比赛**记录（rating-history）拿不到逐题数据，
    但**练习**记录（practice-coding）是公开且完整的，别把前者当成全部。
    """
    if not identifier.isdigit():
        raise FetchError("牛客需要填数字 uid")

    problems: dict[str, int] = {}
    page = 1
    page_size = 100
    # 翻页上限，防止接口行为异常时无限循环。100 页 = 一万条提交，够用。
    max_pages = 100

    while page <= max_pages:
        response = client.get(
            f"https://ac.nowcoder.com/acm/contest/profile/{identifier}/practice-coding",
            params={"page": page, "pageSize": page_size},
        )
        if response.status_code != 200:
            if page == 1:
                raise FetchError(f"牛客返回 {response.status_code}")
            break

        rows = _nowcoder_rows(response.text)
        if not rows:
            if page == 1:
                # 页面能开但没有任何记录：uid 可能不存在，也可能只是没提交过。
                # 无法区分，按「没有数据」处理而不报错。
                break
            break

        for _run_id, title, verdict, submitted_at in rows:
            if verdict != "答案正确":
                continue
            # 题目用标题当键：页面上的链接只有题目 id，但同一题在不同比赛
            # 里 id 不同，标题反而更稳定。
            key = f"nowcoder:{title}"
            epoch = _parse_nowcoder_time(submitted_at)
            if epoch is None:
                continue
            if key not in problems or epoch < problems[key]:
                problems[key] = epoch

        # 本页不足一页，说明到底了。
        if len(rows) < page_size:
            break
        page += 1

    return SolvedSet(platform=Platform.NOWCODER, identifier=identifier, problems=problems)


#: 牛客表格列：运行ID、题目、运行结果、得分、运行时间、使用内存、代码长度、语言、提交时间。
_NOWCODER_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_NOWCODER_CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_NOWCODER_TIME_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})")


def _nowcoder_rows(page: str) -> list[tuple[str, str, str, str]]:
    """从练习页的表格里取出 (运行ID, 题目, 判题结果, 提交时间)。"""
    rows: list[tuple[str, str, str, str]] = []
    for raw in _NOWCODER_ROW_RE.findall(page):
        cells = [
            re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", cell))).strip()
            for cell in _NOWCODER_CELL_RE.findall(raw)
        ]
        # 只认数据行：至少要有运行ID、题目、结果和提交时间。
        if len(cells) < 9:
            continue
        run_id, title, verdict = cells[0], cells[1], cells[2]
        submitted_at = cells[8]
        if not run_id.isdigit() or not _NOWCODER_TIME_RE.match(submitted_at):
            continue
        rows.append((run_id, title, verdict, submitted_at))
    return rows


def _parse_nowcoder_time(value: str) -> int | None:
    """把页面上的时间转成 epoch 秒。

    页面给的是北京时间（无时区标记），所以按东八区解析，不能当 UTC。
    """
    match = _NOWCODER_TIME_RE.match(value)
    if not match:
        return None
    year, month, day, hour, minute, second = (int(part) for part in match.groups())
    try:
        return int(datetime(year, month, day, hour, minute, second, tzinfo=CST).timestamp())
    except ValueError:
        return None


_SOLVED_FETCHERS = {
    Platform.CODEFORCES: fetch_solved_codeforces,
    Platform.ATCODER: fetch_solved_atcoder,
    Platform.LUOGU: fetch_solved_luogu,
    Platform.NOWCODER: fetch_solved_nowcoder,
}


def fetch_solved(platform: str, identifier: str) -> SolvedSet:
    """读一个账号做过的题目。"""
    fetcher = _SOLVED_FETCHERS.get(platform)
    if fetcher is None:
        label = Platform.LABELS.get(platform, platform)
        raise FetchError(f"{label} 不提供解题记录，无法统计")

    headers = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    try:
        with httpx.Client(
            headers=headers, timeout=DEFAULT_TIMEOUT, follow_redirects=False
        ) as client:
            return fetcher(client, identifier)
    except httpx.TimeoutException as exc:
        raise FetchError("请求超时，稍后重试") from exc
    except httpx.HTTPError as exc:
        raise FetchError(f"网络错误：{exc}") from exc
