"""HEAD proxy autonomy - the user's delegated operating policy (2026-09-29).

The user delegated every decision to HEAD except four categories:
  1. spending money (payments, purchases, paid services, paid API keys)
  2. real-money trading (orders on a live broker/exchange account; demo/backtest is fine)
  3. real credentials / account creation
  4. irreversible actions outside the isolated worktree (production deploy, deleting user data,
     messaging people)
plus genuine security violations (control-plane tampering, worktree escape).

Everything else is auto-resolved: scope is NARROWED (never widened beyond what HEAD asked for),
failed calls are retried with feedback, unavailable providers are replaced (visibly, with an audit
record), soft Scope Guard findings are waived or fed back into the repair loop.
"""
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.safety import SafetyPolicy
from schemas.order import OrderSchema

ALIAS_BY_DOMAIN = {"ANTHROPIC": "claude", "OPENAI": "openai", "GOOGLE": "antigravity"}
DOMAIN_PREFERENCE = ("ANTHROPIC", "OPENAI", "GOOGLE")


def enabled(config: Optional[dict]) -> bool:
    return bool(((config or {}).get("autonomy") or {}).get("head_proxy"))


def setting(config: Optional[dict], key: str, default):
    return ((config or {}).get("autonomy") or {}).get(key, default)


# Execution profiles: how much process a request gets. "quick" is for small, well-understood changes (one or two
# milestones, one correction round); "deep" is for ambitious goals. A profile only changes budgets, never the gates.
PROFILE_DEFAULTS = {
    "quick": {"max_steps": 2, "max_repairs": 1, "builder_attempts": 2, "max_consecutive_failures": 2, "max_ai_calls": 12},
    "standard": {"max_steps": 6, "max_repairs": 3, "builder_attempts": 3, "max_consecutive_failures": 3, "max_ai_calls": 36},
    "deep": {"max_steps": 12, "max_repairs": 3, "builder_attempts": 3, "max_consecutive_failures": 4, "max_ai_calls": 72},
}
# max_ai_calls (2026-11-28): the worst case in numbers. Measured on real missions: a clean milestone is 3 calls
# (build, review, verdict+next); retries and failovers add to that. The mission stops at the limit and keeps its work.
PROFILE_TAGS = {"[빠르게]": "quick", "[quick]": "quick", "[깊게]": "deep", "[deep]": "deep", "[표준]": "standard"}


def profile_overrides(config: Optional[dict], name: Optional[str]) -> dict:
    """Autonomy knobs for a named profile (config autonomy.profiles overrides the defaults). Unknown -> {}."""
    if not name:
        return {}
    merged = dict(PROFILE_DEFAULTS.get(name, {}))
    merged.update((setting(config, "profiles", {}) or {}).get(name) or {})
    return {k: v for k, v in merged.items()
            if k in ("max_steps", "max_repairs", "builder_attempts", "max_consecutive_failures", "max_ai_calls")}


def split_profile_tag(text: str):
    """'[빠르게] 캡션 검사기 만들어줘' -> ('quick', '캡션 검사기 만들어줘'); no tag -> (None, text)."""
    stripped = (text or "").lstrip()
    for tag, name in PROFILE_TAGS.items():
        if stripped.lower().startswith(tag.lower()):
            return name, stripped[len(tag):].strip()
    return None, text


# ------------------------------------------------------------------ money / serious-matter detection

SERIOUS_TERMS = (
    "payment", "purchase", "billing", "credit card", "paid plan", "paid service", "subscription fee",
    "real money", "real-money", "live account", "live trading", "real account", "real broker",
    "production deploy", "deploy to production", "credential", "api key", "password", "private key",
    "create an account", "account creation", "legal advice", "regulatory",
    "결제", "구매", "유료", "과금", "실계좌", "실거래", "실제 자금", "실자금", "라이브 계정",
    "자격증명", "비밀번호", "api 키", "계정 생성", "운영 배포", "법률", "규제",
)


def serious_terms_in(text: str) -> List[str]:
    lower = (text or "").lower()
    return [t for t in SERIOUS_TERMS if t in lower]


def is_serious_text(text: str) -> bool:
    """HEAD escalations are kept only for the delegated-exception categories."""
    return (text or "").strip().upper().startswith("USER_DECISION") or bool(serious_terms_in(text))


USER_STOP_MARKERS = (
    "USER_DECISION", "Live money code", "Control plane modified", "Path escapes worktree", "Protected path modified",
    "File modified outside allowed_paths", "PROVIDER_FAILURE", "No usable provider",
    # Reached only when autonomy.waive_scope_caps is off: a dependency / build / permission configuration change is
    # the user's call. (A file-count or diff-size overflow is not listed: the mission re-plans a smaller step.)
    "Dependency configuration modified", "Critical build/architecture configuration modified",
    "Permission configuration modified",
)


SECURITY_STOP_MARKERS = tuple(m for m in USER_STOP_MARKERS if m != "USER_DECISION")


def head_decision_only(detail: str) -> bool:
    """The stop is HEAD's own USER_DECISION with no security stop and no money/credential indicator. HEAD sometimes
    uses it for an ORDER that cannot be met as written (e.g. its own protected_paths lock a broken test fixture) -
    a planning problem the proxy must resolve, not a question for the user."""
    d = detail or ""
    return "USER_DECISION" in d and not any(m in d for m in SECURITY_STOP_MARKERS) and not serious_terms_in(d)


def run_stop_needs_user(detail: str) -> bool:
    """A HUMAN_REQUIRED run stop that the user must see (delegated exception or security). Anything else - missing
    evidence, baseline trouble, a stuck pipeline - is a failed milestone the mission re-plans around."""
    return any(m in (detail or "") for m in USER_STOP_MARKERS) or is_serious_text(detail)


MONEY_CODE_PATTERNS = (
    (re.compile(r"\border_send\s*\("), "broker order submission (order_send)"),
    (re.compile(r"\bOrderSend\s*\("), "broker order submission (OrderSend)"),
    (re.compile(r"\bcreate_(?:market|limit)_(?:buy_|sell_)?order\s*\("), "exchange order submission"),
    (re.compile(r"^\s*(?:import|from)\s+(?:stripe|paypalrestsdk|braintree|iamport|tosspayments)\b"), "payment SDK"),
    (re.compile(r"\b(?:PaymentIntent|Charge)\.create\s*\("), "payment charge"),
)
LIVE_MONEY_PREFIX = "Live money code"
# MetaTrader sources are only compiled by the Director, never executed; an EA places orders only after the user
# attaches it to a chart themselves. The guard exists for code the Director's test run could execute.
NOT_EXECUTED_BY_VERIFICATION = (".mq5", ".mqh", ".mq4")


def money_code_findings(raw_git_diff: str, new_files: Optional[Dict[str, str]] = None) -> List[str]:
    """Added code that could move real money. Scanned BEFORE any test executes the code.
    Returns Scope-Guard-style strings '<LIVE_MONEY_PREFIX>: <path>' (human-approvable per file)."""
    hits: Dict[str, str] = {}
    current = None
    for line in (raw_git_diff or "").splitlines():
        if line.startswith("+++ "):
            target = line[4:].strip()
            current = target[2:] if target.startswith("b/") else target
            continue
        if not line.startswith("+") or current in (None, "/dev/null"):
            continue
        for pattern, label in MONEY_CODE_PATTERNS:
            if pattern.search(line[1:]):
                hits.setdefault(current, label)
    for path, content in (new_files or {}).items():
        for raw in (content or "").splitlines():
            for pattern, label in MONEY_CODE_PATTERNS:
                if pattern.search(raw):
                    hits.setdefault(path, label)
    return [f"{LIVE_MONEY_PREFIX}: {p}" for p in sorted(hits)
            if not str(p).lower().endswith(NOT_EXECUTED_BY_VERIFICATION)]


# ------------------------------------------------------------------ post-build Scope Guard triage

WAIVABLE_PREFIXES = (
    "Max files changed exceeded", "Max diff lines exceeded", "Dependency configuration modified",
    "Critical build/architecture configuration modified", "Permission configuration modified",
)
REPAIRABLE_PREFIXES = ("Unauthorized file deletion", "Unauthorized deletion of existing")


def split_post_build(violations: List[str], waive_caps: bool = False) -> Dict[str, List[str]]:
    """serious (stop) / waivable (HEAD proxy approves) / repairable (back to the Builder).
    Anything unrecognised is serious: fail closed. The cap and configuration findings are waivable only when the
    operator opted in with autonomy.waive_scope_caps (2026-11-20: they used to be waived whenever head_proxy was on,
    which made max_files_changed / max_diff_lines advisory)."""
    out = {"serious": [], "waivable": [], "repairable": []}
    for v in violations or []:
        if waive_caps and v.startswith(WAIVABLE_PREFIXES):
            out["waivable"].append(v)
        elif v.startswith(REPAIRABLE_PREFIXES):
            out["repairable"].append(v)
        else:
            out["serious"].append(v)
    return out


# ------------------------------------------------------------------ baseline tolerance

_FAILED_LINE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)", re.M)


def failing_tests(verification: Optional[dict]) -> set:
    out = ((verification or {}).get("test") or {}).get("output") or ""
    return {m.split(" - ")[0] for m in _FAILED_LINE.findall(out)}


def only_preexisting_failures(baseline: Optional[dict], verification: Optional[dict]) -> Optional[set]:
    """When the untouched baseline already failed: the post-build run counts as green if every failure is one
    that already existed and build/lint passed. Returns the unchanged failures, or None when not applicable."""
    if not baseline or baseline.get("passed") or not verification or verification.get("passed"):
        return None
    if not (verification.get("build") or {}).get("passed", True) or not (verification.get("lint") or {}).get("passed", True):
        return None
    if verification.get("required_outputs_missing"):
        return None
    base, post = failing_tests(baseline), failing_tests(verification)
    if not post or not base or not post.issubset(base):
        return None
    return post


# ------------------------------------------------------------------ ENTRY: narrow, repair, proxy-approve

_PATHLIKE = re.compile(r"^(?:[\w.\-]+/)*[\w.\-]*\.[A-Za-z0-9]{1,8}$|^(?:[\w.\-]+/)+[\w.\-]*$")


def required_output_path(entry: str) -> Optional[str]:
    """The file a required_outputs entry names, or None when the entry is a description rather than a file.
    HEAD writes 'backend/x.py (fix Y)', 'docs/X.md with the new rules' or even 'pytest summary line and ...': only a
    leading repo-relative path-shaped token is a file requirement; prose belongs to the acceptance criteria."""
    text = str(entry or "").strip()
    if not text:
        return None
    first = text.split()[0].rstrip(",;:").replace("\\", "/")
    if _PATHLIKE.match(first) and SafetyPolicy.is_repo_relative_path(first)[0]:
        return first
    return None


def order_text(order: OrderSchema) -> str:
    """What the ORDER asks for (goal/context/criteria) - not its protection lists, which name secrets on purpose."""
    return "\n".join([order.goal or "", order.context or "", *(order.acceptance_criteria or [])])


def _code(reason: str) -> str:
    return reason.split(" ", 1)[0].split("(", 1)[0].strip()


WHOLE_REPO = {"", ".", "./", "/"}


def symbol_deletion(entry: str) -> str:
    """'path/test_x.py::TestA::test_b[param]' -> 'def:test_b' (the guard's symbol form); other entries unchanged."""
    entry = str(entry)
    if "::" not in entry:
        return entry
    name = entry.split("::")[-1].split("[", 1)[0].strip().rstrip("()")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        return entry
    return ("class:" if name[:1].isupper() else "def:") + name


def order_conflicts(order: OrderSchema) -> List[str]:
    """A deliverable the ORDER itself makes impossible: a required output that its own protected_paths cover or that
    its allowed_paths do not include. The Builder can never satisfy such an ORDER, so it goes back to HEAD before a
    run is spent on it. Nothing is widened or unprotected here."""
    problems = []
    for entry in order.required_outputs or []:
        path = required_output_path(entry)
        if not path:
            continue
        if SafetyPolicy.is_path_protected(path, order.protected_paths or []):
            problems.append(f"required output '{path}' is covered by this ORDER's own protected_paths")
        elif order.allowed_paths and not SafetyPolicy.is_path_allowed(path, order.allowed_paths):
            problems.append(f"required output '{path}' is not inside allowed_paths")
    return problems


def sanitize_order(order: OrderSchema, project_path: Optional[str]) -> Tuple[OrderSchema, List[str]]:
    """Representational repairs that can only NARROW authority (plus spelling HEAD's explicit whole-worktree
    intent as the pattern the Scope Guard actually understands). Control plane / .git / protected wins."""
    notes: List[str] = []
    if project_path:
        before = order.model_dump()
        order = SafetyPolicy.normalize_order_paths(order, project_path)
        if order.model_dump() != before:
            notes.append("absolute paths rewritten repo-relative; out-of-project protections dropped")
    data = order.model_dump()

    def valid(p):
        return SafetyPolicy.is_repo_relative_path(p)[0]

    raw_outputs = list(data.get("required_outputs") or [])
    outputs = [required_output_path(p) for p in raw_outputs]
    prose = [p for p, path in zip(raw_outputs, outputs) if path is None]
    if [p for p in outputs if p] != raw_outputs:
        notes.append("required_outputs normalised to file paths; descriptive entries moved to acceptance_criteria")
    data["required_outputs"] = list(dict.fromkeys(p for p in outputs if p))
    data["acceptance_criteria"] = list(data.get("acceptance_criteria") or []) + [
        "Deliverable: " + p for p in prose if "Deliverable: " + p not in (data.get("acceptance_criteria") or [])]

    protected = [p for p in data["protected_paths"] if valid(p)]
    if len(protected) != len(data["protected_paths"]):
        notes.append("dropped unreachable protected paths")
    allowed = []
    for p in data["allowed_paths"]:
        if str(p).strip() in WHOLE_REPO:
            allowed.append("**")
            notes.append("whole-worktree scope expressed as '**' (control plane and protected paths still enforced)")
            continue
        if not valid(p):
            notes.append(f"dropped invalid allowed path {p!r}")
        elif SafetyPolicy.is_control_plane_path(p) or ".git" in [s.lower() for s in Path(p).parts]:
            notes.append(f"dropped control-plane allowed path {p!r}")
        elif SafetyPolicy.is_path_protected(p, protected):
            notes.append(f"dropped allowed path {p!r} that the ORDER itself protects")
        else:
            allowed.append(p)
    if not allowed and data.get("required_outputs"):
        derived = [p for p in data["required_outputs"] if valid(p) and not SafetyPolicy.is_control_plane_path(p)
                   and not SafetyPolicy.is_path_protected(p, protected)]
        if derived:
            allowed = derived
            notes.append("allowed_paths derived from required_outputs")
    # HEAD sometimes names a test to delete as a pytest node id ("tests/test_x.py::test_y"); the deletion guard keys
    # symbols by name, and the path check would reject the whole ORDER as an out-of-worktree path.
    requested = [symbol_deletion(p) for p in data["allowed_deletions"]]
    if requested != list(data["allowed_deletions"]):
        notes.append("allowed_deletions node ids rewritten as symbols: " + ", ".join(
            f"{a} -> {b}" for a, b in zip(data["allowed_deletions"], requested) if a != b))
    deletions = [p for p in requested if (":" in p and "/" not in p and "\\" not in p) or valid(p)]
    if len(deletions) != len(requested):
        notes.append("dropped invalid allowed_deletions")
    # reference_paths is for absolute paths of documents the user supplied; repo files already reach the Builder
    # as source context, and a relative entry makes the Builder reject the whole ORDER.
    refs = [p for p in data.get("reference_paths") or [] if Path(str(p)).is_absolute()]
    if len(refs) != len(data.get("reference_paths") or []):
        notes.append("dropped repo-relative reference_paths (repo files are already supplied to the Builder)")
    data.update(allowed_paths=list(dict.fromkeys(allowed)), protected_paths=protected, allowed_deletions=deletions,
                reference_paths=refs)
    return OrderSchema(**data), notes


AUTO_ENTRY_CODES = {"AMBIGUOUS_SCOPE", "SENSITIVE_FILE_IN_SCOPE", "PROTECTED_PATH_INSIDE_ALLOWED", "DESTRUCTIVE_CHANGE"}
JUDGED_ENTRY_CODES = {"HIGH_RISK", "EXPLICIT_HUMAN_APPROVAL_REQUESTED"}


def proxy_verdict(head, question: str, evidence: Dict[str, Any], cwd: Optional[str] = None) -> Tuple[bool, str, str]:
    """(approve, reason, source). HEAD decides as the user's proxy; if HEAD cannot be reached the user's
    written policy is applied deterministically (escalate only on money/credential indicators)."""
    fn = getattr(head, "proxy_decide", None)
    last = None
    if fn is not None:
        for _ in range(2):
            try:
                res = fn(question, evidence, cwd=cwd) or {}
                decision = str(res.get("decision", "")).upper()
                if decision in ("APPROVE", "ESCALATE"):
                    return decision == "APPROVE", str(res.get("reason", ""))[:500], "HEAD"
                last = f"unexpected decision {decision!r}"
            except Exception as exc:
                last = str(exc)[:200]
    hits = serious_terms_in(question + " " + str(evidence.get("scan_text", "")))
    reason = ("HEAD proxy unavailable (" + str(last) + "); policy fallback: "
              + ("money/credential indicators " + ", ".join(hits) if hits else "no money/credential indicators"))
    return not hits, reason, "POLICY"


def rescope(order: OrderSchema, project_path: str, head, attempts: int = 2) -> Tuple[OrderSchema, List[str]]:
    """Empty write scope with a nonzero write budget: HEAD names the minimal repo-relative paths it needs."""
    notes, errors = [], []
    for _ in range(attempts):
        try:
            proposal = head.create_order(order.goal, {
                "project_path": str(project_path), "original_order": order.model_dump(),
                "instruction": "Define allowed_paths ONLY: the minimal repo-relative files/subtrees (e.g. 'src/**', "
                               "'tests/test_x.py') this goal must write. No absolute paths, no '..', no .git/.agents.",
                "previous_errors": errors})
        except Exception as exc:
            errors.append(str(exc)[:200])
            continue
        candidate = order.model_copy(deep=True)
        candidate.allowed_paths = list(proposal.allowed_paths or [])
        candidate, n = sanitize_order(candidate, project_path)
        if candidate.allowed_paths:
            notes.extend(n + ["HEAD re-scoped allowed_paths to " + ", ".join(candidate.allowed_paths)])
            return candidate, notes
        errors.append("HEAD returned no usable allowed_paths")
    return order, notes + errors


def entry_gate(order: OrderSchema, project_path: Optional[str], head, cwd: Optional[str] = None) -> Dict[str, Any]:
    """Returns {'order', 'notes', 'approved': [reasons], 'escalate': None | 'APPROVAL_REQUIRED: ...'}."""
    order, notes = sanitize_order(order, project_path)
    reasons = SafetyPolicy.assess_order_for_auto_run(order)
    if (any(_code(r) == "AMBIGUOUS_SCOPE" for r in reasons) and not order.allowed_paths
            and head is not None and project_path):
        order, extra = rescope(order, project_path, head)
        notes.extend(extra)
        reasons = SafetyPolicy.assess_order_for_auto_run(order)
    if not order.allowed_paths and (order.max_files_changed or order.max_diff_lines):
        return {"order": order, "notes": notes, "approved": [],
                "escalate": "APPROVAL_REQUIRED: AMBIGUOUS_SCOPE (HEAD could not name any writable path)"}
    blocking = [r for r in reasons if _code(r) not in AUTO_ENTRY_CODES | JUDGED_ENTRY_CODES]
    if blocking:
        return {"order": order, "notes": notes, "approved": [], "escalate": "APPROVAL_REQUIRED: " + "; ".join(blocking)}
    judged = [r for r in reasons if _code(r) in JUDGED_ENTRY_CODES]
    if judged:
        approve, why, source = proxy_verdict(
            head, "The ORDER below was flagged " + ", ".join(judged) + ". Approve it on the user's behalf?",
            {"order": order.model_dump(), "scan_text": order_text(order)}, cwd=cwd or project_path)
        notes.append(f"HEAD proxy ({source}): {'APPROVE' if approve else 'ESCALATE'} - {why}")
        if not approve:
            return {"order": order, "notes": notes, "approved": [],
                    "escalate": f"APPROVAL_REQUIRED: USER_DECISION_REQUIRED ({why[:300]})"}
    return {"order": order, "notes": notes, "approved": reasons, "escalate": None}


# ------------------------------------------------------------------ Builder retry feedback / provider choice

def builder_retry_feedback(error: str, order: OrderSchema) -> str:
    e = (error or "").lower()
    tips = []
    if any(k in e for k in ("json", "unterminated", "expecting", "escape", "delimiter", "output token", "cut off")):
        tips.append("Your previous reply was not complete valid JSON (probably too long). Return ONE compact valid "
                    "JSON object, escape backslashes and quotes, and keep it short: use \"edits\" (exact search/replace) "
                    "instead of rewriting whole existing files, fewer/smaller files. If the full "
                    "change is too large, deliver a working core subset now and list the remainder in summary.")
    if "empty" in e:
        tips.append("Your previous reply was empty. Return the JSON proposal.")
    if "path denied" in e:
        tips.append(f"Write ONLY inside allowed_paths {order.allowed_paths}; never inside protected paths "
                    f"{order.protected_paths} or .git/.agents/GEMINI.md.")
    if "exactly once" in e or "edit target" in e or "invalid edit" in e:
        tips.append("An edit anchor did not match: copy each \"old\" text verbatim from the file content shown "
                    "(same indentation and line breaks), make it long enough to be unique, or send the complete file.")
    if "files list" in e or "duplicate" in e:
        tips.append(f"Return between 1 and {order.max_files_changed} distinct files with complete contents.")
    if "timeout" in e or "timed out" in e:
        tips.append("The previous attempt timed out: deliver a smaller, complete, working increment.")
    if not tips:
        tips.append("Previous attempt failed: " + (error or "")[:300] + ". Fix the cause and return the JSON proposal.")
    return "BUILDER RETRY NOTICE: " + " ".join(tips)


def pick_alternative(pm, exclude: set, avoid: Optional[set] = None) -> Optional[str]:
    """Another usable provider alias for a role (never one in `exclude`; prefers ones not in `avoid`)."""
    try:
        usable = set(pm.usable_domains()) if pm is not None else set(DOMAIN_PREFERENCE)
    except Exception:
        usable = set(DOMAIN_PREFERENCE)
    avoid = avoid or set()
    cands = [d for d in DOMAIN_PREFERENCE if d in usable and d not in exclude]
    cands.sort(key=lambda d: d in avoid)
    return ALIAS_BY_DOMAIN[cands[0]] if cands else None
