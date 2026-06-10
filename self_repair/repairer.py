from __future__ import annotations
from typing import Optional
"""
自己修復モジュール: 問題を検知したらClaude CLIを呼び出してコードを修正する
"""
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from self_repair.diagnostics import Problem, ProblemType

BASE_DIR = Path(__file__).parent.parent
REPAIR_LOG_PATH = BASE_DIR / 'logs' / 'repair_history.jsonl'
BACKUP_DIR = BASE_DIR / 'backups'
RATE_LIMIT_FILE = BASE_DIR / 'logs' / 'repair_rate_limit.txt'
COOLDOWN_FILE = BASE_DIR / 'logs' / 'repair_cooldown.json'
REPAIR_COUNT_FILE = BASE_DIR / 'logs' / 'repair_count.json'  # 再起動でも失わないカウンタ (穴12修正)

# 1日あたりの最大修復試行回数（成功・失敗問わずカウント）
MAX_REPAIRS_PER_DAY = 12

_repair_count_today = 0
_repair_count_date = ''


def _load_repair_count() -> None:
    """起動時にファイルからカウンタを復元する (穴12修正)"""
    global _repair_count_today, _repair_count_date
    try:
        if REPAIR_COUNT_FILE.exists():
            data = json.loads(REPAIR_COUNT_FILE.read_text(encoding='utf-8'))
            _repair_count_date = data.get('date', '')
            _repair_count_today = data.get('count', 0)
            logging.debug(f'[Repair] カウンタ復元: {_repair_count_date} → {_repair_count_today}回')
    except Exception as e:
        logging.debug(f'[Repair] カウンタ復元失敗（初回起動等）: {e}')


def _save_repair_count() -> None:
    """カウンタをファイルに書き出す (穴12修正)"""
    try:
        REPAIR_COUNT_FILE.parent.mkdir(exist_ok=True)
        tmp = REPAIR_COUNT_FILE.with_suffix('.tmp')
        tmp.write_text(
            json.dumps({'date': _repair_count_date, 'count': _repair_count_today}),
            encoding='utf-8'
        )
        tmp.replace(REPAIR_COUNT_FILE)
    except Exception as e:
        logging.debug(f'[Repair] カウンタ保存失敗: {e}')


# モジュールロード時にカウンタ復元
_load_repair_count()

# Claude CLIパスのキャッシュ
_cached_claude_cmd: Optional[str] = None
_cached_claude_use_shell: bool = False
_cache_verified_at: float = 0
_CACHE_TTL = 900  # 15分間のみキャッシュ（PATH変更に即応）

# タイムアウトの絶対下限（無限ループ防止: この値より短いタイムアウトは一切設定不可）
MIN_TIMEOUT_SECONDS = 120  # 絶対最小値 = 2分（CLIの起動+応答に最低限必要）


# ─────────────────────────────────────────────────────────────
# CLI検出（超高速版：検証完全スキップ、多段フォールバック）
# ─────────────────────────────────────────────────────────────

def _get_claude_cmd() -> tuple[Optional[str], bool]:
    """
    Claude CLIのパスを自動検出する（Windows対応、改善版 v4）。
    Returns: (コマンド文字列またはNone, shell=Trueが必要か)

    改善 v4:
    - キャッシュTTL短縮 (900s=15分)
    - 9段階の多段フォールバック戦略
    - .cmd.cmd 二重拡張子対応（Windows npm quirk）
    - Node.js binディレクトリ直接検索
    - %APPDATA%\npm、%ProgramFiles(x86)% 対応
    - 詳細なdebugログで検出過程を記録
    """
    global _cached_claude_cmd, _cached_claude_use_shell, _cache_verified_at

    # キャッシュが新鮮 → 検証なしで即座に返す
    if _cached_claude_cmd and (time.time() - _cache_verified_at) < _CACHE_TTL:
        logging.debug(f'[Repair] Claude CLI キャッシュから復帰: {_cached_claude_cmd}')
        return _cached_claude_cmd, _cached_claude_use_shell

    def _save_cache(cmd: str, use_shell: bool) -> tuple[str, bool]:
        global _cached_claude_cmd, _cached_claude_use_shell, _cache_verified_at
        _cached_claude_cmd = cmd
        _cached_claude_use_shell = use_shell
        _cache_verified_at = time.time()
        logging.info(f'[Repair] Claude CLI 検出: {cmd} (shell={use_shell})')
        return cmd, use_shell

    def _check_file_exists(path: str) -> bool:
        """ファイルが存在するかチェック"""
        try:
            return os.path.isfile(path)
        except Exception:
            return False

    # 戦略1: shutil.which()で検索（最速・最信頼）
    found = shutil.which('claude')
    if found:
        use_shell = found.lower().endswith(('.cmd', '.bat'))
        logging.debug(f'[Repair] Strategy 1: shutil.which()で検出: {found}')
        return _save_cache(found, use_shell)

    # 戦略2: npm_modules の .bin（ローカルインストール、Windows優先）
    bin_paths = [
        os.path.join(BASE_DIR, 'node_modules', '.bin', 'claude'),
        os.path.join(BASE_DIR, 'node_modules', '.bin', 'claude.cmd'),
        os.path.join(BASE_DIR, 'node_modules', '.bin', 'claude.bat'),
    ]
    for bin_path in bin_paths:
        if _check_file_exists(bin_path):
            use_shell = bin_path.lower().endswith(('.cmd', '.bat'))
            logging.debug(f'[Repair] Strategy 2: ローカル npm_modules: {bin_path}')
            return _save_cache(bin_path, use_shell)

    # 戦略3: NPM_CONFIG_PREFIX
    npm_prefix = os.environ.get('NPM_CONFIG_PREFIX', '')
    if npm_prefix:
        for suffix in ['', '.cmd', '.bat']:
            npm_claude = os.path.join(npm_prefix, 'bin', f'claude{suffix}')
            if _check_file_exists(npm_claude):
                use_shell = suffix in ['.cmd', '.bat']
                logging.debug(f'[Repair] Strategy 3: NPM_CONFIG_PREFIX: {npm_claude}')
                return _save_cache(npm_claude, use_shell)

    # 戦略4: YARN_CONFIG_HOME
    yarn_home = os.environ.get('YARN_CONFIG_HOME', '')
    if yarn_home:
        for suffix in ['', '.cmd', '.bat']:
            yarn_claude = os.path.join(yarn_home, 'bin', f'claude{suffix}')
            if _check_file_exists(yarn_claude):
                use_shell = suffix in ['.cmd', '.bat']
                logging.debug(f'[Repair] Strategy 4: YARN_CONFIG_HOME: {yarn_claude}')
                return _save_cache(yarn_claude, use_shell)

    # 戦略5: グローバル npm インストール位置（Windows、強化版）
    if sys.platform == 'win32':
        appdata = os.path.expandvars('%APPDATA%')
        localappdata = os.path.expandvars('%LOCALAPPDATA%')
        possible_paths = [
            os.path.join(appdata, 'npm', 'claude'),
            os.path.join(appdata, 'npm', 'claude.cmd'),
            os.path.join(appdata, 'npm', 'claude.bat'),
            os.path.join(appdata, 'npm', 'claude.cmd.cmd'),  # 二重拡張子対応
            os.path.join(localappdata, 'npm', 'claude'),      # LocalAppData（一般的インストール先）
            os.path.join(localappdata, 'npm', 'claude.cmd'),
            os.path.join(localappdata, 'npm', 'claude.bat'),
            os.path.expandvars(r'%ProgramFiles%\nodejs\claude'),
            os.path.expandvars(r'%ProgramFiles%\nodejs\claude.cmd'),
            os.path.expandvars(r'%ProgramFiles%\nodejs\claude.exe'),
            os.path.expandvars(r'%ProgramFiles(x86)%\nodejs\claude'),
            os.path.expandvars(r'%ProgramFiles(x86)%\nodejs\claude.cmd'),
        ]
        for path in possible_paths:
            if _check_file_exists(path):
                use_shell = path.lower().endswith(('.cmd', '.bat'))
                logging.debug(f'[Repair] Strategy 5: Windows グローバル npm: {path}')
                return _save_cache(path, use_shell)

    # 戦略6: Scoop（Windows パッケージマネージャー）
    if sys.platform == 'win32':
        scoop_paths = [
            os.path.expandvars(r'%USERPROFILE%\scoop\apps\claude\current\claude'),
            os.path.expandvars(r'%USERPROFILE%\scoop\apps\claude\current\claude.cmd'),
        ]
        for path in scoop_paths:
            if _check_file_exists(path):
                use_shell = path.lower().endswith('.cmd')
                logging.debug(f'[Repair] Strategy 6: Scoop: {path}')
                return _save_cache(path, use_shell)

    # 戦略7: Chocolatey（Windows パッケージマネージャー）
    if sys.platform == 'win32':
        choco_path = os.path.expandvars(r'%ProgramFiles%\claude\bin\claude.exe')
        if _check_file_exists(choco_path):
            logging.debug(f'[Repair] Strategy 7: Chocolatey: {choco_path}')
            return _save_cache(choco_path, False)

    # 戦略8: PATH 環境変数を直接検索（Windows、詳細版）
    if sys.platform == 'win32':
        path_env = os.environ.get('PATH', '')
        pathext_env = os.environ.get('PATHEXT', '.COM;.EXE;.BAT;.CMD').split(os.pathsep)

        for path_dir in path_env.split(os.pathsep):
            if not path_dir:
                continue
            # PATHEXT に含まれる全拡張子を試す
            for suffix in ['', '.cmd', '.bat', '.cmd.cmd', '.exe'] + pathext_env:
                full_path = os.path.join(path_dir, f'claude{suffix}')
                if _check_file_exists(full_path):
                    use_shell = suffix.lower() in ['', '.cmd', '.bat', '.cmd.cmd']
                    logging.debug(f'[Repair] Strategy 8: PATH検索: {full_path}')
                    return _save_cache(full_path, use_shell)

    # 戦略9: Node.js グローバル bin ディレクトリ（Windows、追加）
    if sys.platform == 'win32':
        node_path = shutil.which('node')
        if node_path:
            node_bin_dir = os.path.dirname(node_path)
            for suffix in ['', '.cmd', '.bat', '.exe']:
                claude_path = os.path.join(node_bin_dir, f'claude{suffix}')
                if _check_file_exists(claude_path):
                    use_shell = suffix in ['', '.cmd', '.bat']
                    logging.debug(f'[Repair] Strategy 9: Node.js binディレクトリ: {claude_path}')
                    return _save_cache(claude_path, use_shell)

    # 戦略10: npm-cli.js 直接実行（node npm-cli.js）
    npm_cli_paths = [
        os.path.join(BASE_DIR, 'node_modules', 'npm', 'bin', 'npm-cli.js'),
        os.path.expandvars(r'%APPDATA%\npm\node_modules\npm\bin\npm-cli.js'),
    ]
    for npm_cli_path in npm_cli_paths:
        if _check_file_exists(npm_cli_path):
            # claude のグローバルパスを取得
            try:
                result = subprocess.run(
                    ['node', npm_cli_path, 'root', '-g'],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode == 0:
                    npm_root = result.stdout.strip()
                    claude_exe = os.path.join(npm_root, '.bin', 'claude.cmd')
                    if _check_file_exists(claude_exe):
                        logging.debug(f'[Repair] Strategy 10: npm-cli.js から発見: {claude_exe}')
                        return _save_cache(claude_exe, True)
            except Exception:
                pass

    # 最終フォールバック: shell=True で "claude" を実行（環境変数に依存）
    logging.warning('[Repair] Claude CLI自動検出失敗（10戦略すべて失敗）。shell=True で "claude" を実行します')
    return _save_cache('claude', True)


# ─────────────────────────────────────────────────────────────
# ファイル操作
# ─────────────────────────────────────────────────────────────

def _read_file_safe(path: Path) -> str:
    """ファイルを読み込む（BOM対応・例外時は空文字列）"""
    for enc in ('utf-8-sig', 'utf-8', 'cp932'):
        try:
            text = path.read_text(encoding=enc)
            # JSON経由で混入するBOM文字を除去
            return text.lstrip('\ufeff')
        except UnicodeDecodeError:
            continue
        except Exception as e:
            logging.warning(f'[Repair] ファイル読み込み失敗({path}): {e}')
            return ''
    return ''


def _backup_file(filepath: Path) -> Optional[Path]:
    """修復前にファイルをバックアップする（最新10件を保持）"""
    try:
        BACKUP_DIR.mkdir(exist_ok=True)
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_path = BACKUP_DIR / f'{filepath.name}.{ts}.bak'
        shutil.copy2(filepath, backup_path)
        logging.info(f'[Repair] バックアップ作成: {backup_path}')

        existing = sorted(
            BACKUP_DIR.glob(f'{filepath.name}.*.bak'),
            key=lambda p: p.stat().st_mtime
        )
        for old in existing[:-10]:
            try:
                old.unlink()
            except Exception:
                pass

        return backup_path
    except Exception as e:
        logging.error(f'[Repair] バックアップ失敗: {e}')
        return None


def _find_latest_snapshot(source: str) -> str:
    """該当ソースの最新スナップショットを返す（なければ空文字）"""
    snapshot_dir = BASE_DIR / 'logs' / 'snapshots'
    prefix = source.lower()
    try:
        snaps = sorted(snapshot_dir.glob(f'{prefix}_*.html'), key=lambda p: p.stat().st_mtime)
        if snaps:
            content = snaps[-1].read_text(encoding='utf-8', errors='replace')
            # 2000文字に短縮（プロンプト削減）
            if len(content) > 2000:
                content = content[:2000] + '\n<!-- truncated -->'
            return f'=== スナップショット: {snaps[-1].name} ===\n{content}'
    except Exception:
        pass
    return ''


# ─────────────────────────────────────────────────────────────
# ASTベース 関数抽出・外科的置換（サージカルパッチの核心）
# ─────────────────────────────────────────────────────────────

def _extract_function_source(file_path: Path, func_name: str) -> Optional[str]:
    """
    ASTを使って指定関数のソースコードを完全に抽出する。
    ファイル全体を渡す必要はない — 対象関数だけをClaudeに見せる。
    """
    import ast as _ast
    try:
        source = file_path.read_text(encoding='utf-8')
        tree = _ast.parse(source)
        lines = source.splitlines(keepends=True)

        for node in _ast.walk(tree):
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                if node.name == func_name:
                    start = node.lineno - 1      # 0-indexed
                    end = node.end_lineno        # 0-indexed inclusive
                    func_source = ''.join(lines[start:end])
                    logging.debug(
                        f'[Repair] 関数抽出: {func_name} '
                        f'(行{node.lineno}–{node.end_lineno}, {len(func_source)}bytes)'
                    )
                    return func_source

        logging.warning(f'[Repair] 関数 {func_name} が {file_path.name} に見つかりません')
        return None
    except Exception as e:
        logging.warning(f'[Repair] 関数抽出失敗 {func_name}: {e}')
        return None


def _replace_function_in_file(file_path: Path, func_name: str, new_func_code: str) -> bool:
    """
    ASTを使ってファイル内の指定関数だけを new_func_code で置換する。
    ファイル全体を書き直さず、対象関数の行範囲だけを差し替える。
    置換後のファイル全体が構文的に正しいことをast.parseで確認してから書き込む。
    """
    import ast as _ast
    try:
        source = file_path.read_text(encoding='utf-8')
        tree = _ast.parse(source)
        lines = source.splitlines(keepends=True)

        for node in _ast.walk(tree):
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                if node.name == func_name:
                    start = node.lineno - 1   # 0-indexed
                    end = node.end_lineno     # 0-indexed inclusive

                    # 置換: [0..start) + new_func + [end..)
                    replacement = new_func_code.rstrip('\n') + '\n'
                    new_lines = lines[:start] + [replacement] + lines[end:]
                    new_source = ''.join(new_lines)

                    # 置換後のファイル全体が構文OKか確認
                    try:
                        _ast.parse(new_source)
                    except SyntaxError as se:
                        logging.error(
                            f'[Repair] 置換後ファイルに構文エラー: {se.msg} (行{se.lineno})'
                        )
                        return False

                    # アトミック書き込み
                    tmp = file_path.with_suffix(file_path.suffix + '.tmp')
                    tmp.write_text(new_source, encoding='utf-8')
                    os.replace(tmp, file_path)
                    logging.info(
                        f'[Repair] 関数置換成功: {func_name} in {file_path.name} '
                        f'(旧{end - start}行 → 新{new_func_code.count(chr(10))+1}行)'
                    )
                    return True

        logging.error(f'[Repair] 関数 {func_name} が {file_path.name} に見つかりません')
        return False
    except Exception as e:
        logging.error(f'[Repair] 関数置換失敗 {func_name}: {e}')
        return False


# 問題タイプ → (ファイルパス相対, 対象関数名) のマッピング
# ファイル全体ではなく「修正すべき関数」だけをClaudeに渡す。
PROBLEM_TARGET_FUNCTIONS: dict[object, tuple[str, str]] = {
    # ProblemTypeはここでは文字列キーで比較（循環import回避のため遅延解決）
    'yahoo_zero_results':      ('scrapers/yahoo_scraper.py',    'scrape_yahoo'),
    'google_zero_results':     ('scrapers/google_scraper.py',   'scrape_google'),
    'meta_session_expired':    ('scrapers/meta_scraper.py',     'check_meta_login'),
    # company_extraction_low は自己修復対象から除外 (穴1修正):
    # find_company_info は283行の複雑関数でClaudeが短縮版を返しやすく
    # tel:リンク抽出・_extract_phones_from_soup等の重要機能が削除される。
    # 'company_extraction_low': ('processors/company_finder.py', 'find_company_info'),
}


# ─────────────────────────────────────────────────────────────
# プロンプト構築（最適化版）
# ─────────────────────────────────────────────────────────────

def _read_recent_log_lines(n: int) -> str:
    """scraper.log から直近 n 行を取得する（ログ行のみ抽出・Claude応答混入防止）"""
    # ログ行パターン: "YYYY-MM-DD HH:MM:SS [level]" または "[Repair]" を含む行のみ
    LOG_LINE_RE = re.compile(
        r'^\d{4}-\d{2}-\d{2}|^\[Repair\]|^\[scraper\]|'
        r'\[ERROR\]|\[WARNING\]|\[INFO\]|\[DEBUG\]|'
        r'ERROR:|WARNING:|INFO:|DEBUG:'
    )
    for log_name in ('scraper.log', 'error.log'):
        log_path = BASE_DIR / 'logs' / log_name
        try:
            raw = log_path.read_text(encoding='utf-8', errors='replace').strip().splitlines()
            # ログ行だけ残す（Claude応答テーブルなどを除外）
            filtered = [l for l in raw if LOG_LINE_RE.search(l)]
            if filtered:
                return '\n'.join(filtered[-n:])
        except Exception:
            continue
    return '(ログ取得失敗)'


def _build_prompt(problem: Problem, func_name: str, func_source: str) -> str:
    """
    関数レベルの修復プロンプト。
    ファイル全体ではなく「問題のある関数」だけをClaudeに渡す。
    → 切り捨てゼロ・Claudeは対象を100%把握・生成量も最小
    """
    recent_errors = _read_recent_log_lines(10)

    # スナップショット（セレクタ問題の場合はHTMLサンプルを添付）
    snapshot_section = ''
    ptype = problem.type.value
    if ptype == 'yahoo_zero_results':
        snap = _find_latest_snapshot('yahoo')
        if snap:
            snapshot_section = f'\n## 最新Yahoo検索結果HTMLサンプル\n```html\n{snap}\n```\n'
    elif ptype == 'google_zero_results':
        snap = _find_latest_snapshot('google')
        if snap:
            snapshot_section = f'\n## 最新Google検索結果HTMLサンプル\n```html\n{snap}\n```\n'

    extra_context = ''
    if 'company_finder' in problem.affected_file:
        extra_context = """
## 背景
- 対象LPはReact/Vue.js等のJavaScript SPA
- requests.get()では会社名・電話番号が描画されないため、
  特商法ページ（/tokutei, /legal, /company等）を優先的に探す
- 現状の取得率10%未満は異常。30〜50%を目指す。
"""

    return f"""You are a Python repair agent. Fix the function below and output ONLY the replacement function.

## Problem
{problem.description}

## Target: `{func_name}` in `{problem.affected_file}`
{extra_context}
```python
{func_source}
```

## Recent error log
```
{recent_errors}
```
{snapshot_section}
## Output rules (STRICT)
1. Output ONLY a single ```python block containing the fixed function
2. Do NOT output the entire file — just the one function
3. Keep the exact function signature (name, parameters, return type)
4. Add any new imports as local imports inside the function body
5. The function must be syntactically valid Python

```python
def {func_name}(...):
    # your fixed implementation here
```
"""


# ─────────────────────────────────────────────────────────────
# コード抽出・検証（超高柔軟性版・複数フォールバック）
# ─────────────────────────────────────────────────────────────

def _extract_code_from_response(response: str) -> Optional[str]:
    """
    Claude CLIの応答からPythonコードを抽出する（最適化版 v9・20+パターン対応）

    戦略: 優先度付き20パターン以上 → 最長の有効候補を選択
    1. 標準マークダウン（```python）
    2. プロンプトマーカー（# 修正済みコード）
    3. XML/HTMLタグ
    4. マークダウン（言語指定なし、Pythonキーワード検証）
    5. チルダブロック
    6. インデント済みコード
    7. def/import/class で始まるコード
    8. セクションヘッダー（## Fixed Code など）
    9. JSON形式（"code": "..." ）
    10. コメント囲み（# Begin code ... # End code）
    11. HTMLコードタグ（<code>, <pre>）
    12. Liquid/Jinja2 テンプレートタグ（{% code %}, {{ code }}）
    13. 箇条書き形式（- ` で始まるコードブロック）
    14. リスト形式（番号付きリスト内のコード）
    15. マークダウンコードスニペット（`code` または ```code```）
    16. URLエンコード/エスケープ応答の復元
    17. 引用符で囲まれたコード（「"code": "..."」から自動復元）
    18. コロン区切り応答（「Code: ... Code end」）
    19. セクション分割マーカー（「---」で区切られたセクション）
    20. 最後のフォールバック: 最も長い```ブロック

    最小要件: 150文字 以上、Pythonキーワード複数個含有
    """
    if not response or len(response) < 50:
        logging.debug('[Repair] 応答が短い（<50文字）')
        return None

    candidates = []
    MIN_CODE_SIZE = 150  # 最小150文字

    # Pythonキーワード検証用（拡張版、より多くのキーワード）
    PYTHON_KEYWORDS = {
        # 制御フロー
        'def ', 'class ', 'return ', 'yield ', 'if ', 'for ', 'while ', 'try:', 'except', 'else:', 'elif ',
        'with ', 'raise ', 'assert ', 'break', 'continue', 'pass', 'lambda',
        # インポート
        'import ', 'from ', 'as ',
        # 変数・操作
        'global ', 'nonlocal ', 'del ', '= ',
        # 関数呼び出し・属性
        'logging.', 'subprocess.', '.read_text', '.write_text', '.mkdir', '.unlink',
        '.glob', '.exists', '.is_file', '.is_dir',
        'Path(', 'os.', 'sys.', 'json.', 'time.', 're.', 'shutil.', 'tempfile.',
        # async
        'async def', 'await ',
        # 特殊
        '__name__', 'Exception', 'try', 'except', '@property', '@staticmethod',
        # ad_scraper特有
        'logging.info', 'logging.error', 'logging.warning', 'logging.debug',
        'subprocess.run', 'Path(', 'self_repair', 'Problem', 'ProblemType',
    }

    def _has_python_keyword(code: str) -> bool:
        """Pythonキーワードが含まれているか確認（複数キーワード推奨）"""
        count = sum(1 for kw in PYTHON_KEYWORDS if kw in code)
        return count >= 2  # 最低2個のキーワードが必要

    def _validate_candidate(code: str, min_lines: int = 3) -> bool:
        """候補コードの最小検証"""
        if len(code) < MIN_CODE_SIZE:
            return False
        if len(code.splitlines()) < min_lines:
            return False
        return _has_python_keyword(code)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # パターン1: 標準的なPythonコードブロック（優先度最高）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    python_variants = [
        'python', 'Python', 'python3', 'py',  # 主要
        'PYTHON', 'Python3', 'PY', 'Py', 'pY', 'python3.9', 'python3.10', 'python3.11',  # ケースバリエーション
    ]
    for variant in python_variants:
        for match in re.finditer(rf'```{variant}\s*\n(.*?)\n```', response, re.DOTALL):
            code = match.group(1).strip()
            if _validate_candidate(code):
                candidates.append((code, f'std-python-{variant}', len(code)))

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # パターン2: プロンプトマーカー直後のコード抽出
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    prompt_markers = [
        '# 修正済みコード（', '# 修正済みコード', '# 修正後のファイル全体',
        '# Fixed code', '修正後のコード全体', '# Complete fixed code',
        '修正済みコード（repairer.py全体）',
    ]
    for marker in prompt_markers:
        match = re.search(re.escape(marker) + r'\s*\n(.*)', response, re.DOTALL | re.IGNORECASE)
        if match:
            remainder = match.group(1).strip()
            m = re.search(r'```(?:python)?\s*\n(.*?)\n```', remainder, re.DOTALL)
            if m:
                code = m.group(1).strip()
                if _validate_candidate(code):
                    candidates.append((code, f'prompt-marker-{marker[:20]}', len(code)))

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # パターン3: XML/HTMLタグ
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    for tag in ['answer', 'code', 'python', 'solution', 'content', 'text']:
        match = re.search(rf'<{tag}>\s*(.*?)\s*</{tag}>', response, re.DOTALL | re.IGNORECASE)
        if match:
            code = match.group(1).strip()
            if _validate_candidate(code):
                candidates.append((code, f'xml-{tag}', len(code)))

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # パターン4: マークダウン（言語指定なし、Pythonキーワード検証）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    for match in re.finditer(r'```\s*\n(.*?)\n```', response, re.DOTALL):
        code = match.group(1).strip()
        if _validate_candidate(code):
            candidates.append((code, 'generic-markdown', len(code)))

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # パターン5: チルダブロック
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    for match in re.finditer(r'~~~(?:python|py)?\s*\n(.*?)\n~~~', response, re.DOTALL):
        code = match.group(1).strip()
        if _validate_candidate(code):
            candidates.append((code, 'tilde-block', len(code)))

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # パターン6: インデント済みコードブロック
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    match = re.search(r'((?:(?:^    |\t).+$\n?)+)', response, re.MULTILINE)
    if match:
        code = match.group(1).strip()
        if _validate_candidate(code):
            candidates.append((code, 'indented-block', len(code)))

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # パターン7: def/import/class で始まるコード
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    for keyword_start in [r'^(def\s+\w+.*)', r'^(class\s+\w+.*)', r'^((?:import|from)\s+.*)']:
        match = re.search(keyword_start + r'(?:\n(?!^[A-Z]|\Z|```|\*\*).*)*', response, re.MULTILINE)
        if match:
            code = match.group(1).strip()
            if _validate_candidate(code):
                candidates.append((code, f'keyword-start-{keyword_start[2:8]}', len(code)))

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # パターン8: セクションヘッダー（## Fixed Code など）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    section_markers = [
        '## Fixed Code', '## Complete Code', '## Updated Code',
        '# Complete fixed code', '# Full solution',
        '## Solution', '## Implementation',
    ]
    for marker in section_markers:
        match = re.search(re.escape(marker) + r'\s*\n+\s*```(?:python)?\s*\n(.*?)\n```', response, re.DOTALL | re.IGNORECASE)
        if match:
            code = match.group(1).strip()
            if _validate_candidate(code):
                candidates.append((code, f'section-{marker[:20]}', len(code)))

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # パターン9: JSON形式（"code": "..."）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    for match in re.finditer(r'"code"\s*:\s*"((?:[^"\\]|\\.)*)"', response, re.DOTALL):
        code_str = match.group(1)
        # エスケープシーケンスをデコード
        try:
            code = code_str.encode('utf-8').decode('unicode_escape').replace(r'\n', '\n')
            if _validate_candidate(code):
                candidates.append((code, 'json-code', len(code)))
        except Exception:
            pass

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # パターン10: コメント囲み（# Begin code ... # End code）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    for begin_marker, end_marker in [
        ('# Begin code', '# End code'),
        ('# BEGIN', '# END'),
        ('# BEGIN CODE', '# END CODE'),
        ('### Begin', '### End'),
    ]:
        match = re.search(re.escape(begin_marker) + r'\s*\n(.*?)\n' + re.escape(end_marker), response, re.DOTALL | re.IGNORECASE)
        if match:
            code = match.group(1).strip()
            if _validate_candidate(code):
                candidates.append((code, f'comment-{begin_marker[:10]}', len(code)))

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # パターン11: HTMLコードタグ（<code>, <pre>）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    for tag in ['code', 'pre']:
        match = re.search(rf'<{tag}[^>]*>\s*(.*?)\s*</{tag}>', response, re.DOTALL | re.IGNORECASE)
        if match:
            code_str = match.group(1).strip()
            # HTMLエンティティをデコード
            code = code_str.replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&')
            if _validate_candidate(code):
                candidates.append((code, f'html-{tag}', len(code)))

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # パターン12: Liquid/Jinja2 テンプレートタグ
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    for tag_pattern in [r'{%\s*code\s*%}(.*?){%\s*endcode\s*%}', r'{{\s*code\s*}}(.*?){{/code}}']:
        for match in re.finditer(tag_pattern, response, re.DOTALL | re.IGNORECASE):
            code = match.group(1).strip()
            if _validate_candidate(code):
                candidates.append((code, 'template-tag', len(code)))

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # パターン13: 箇条書き形式（- ``` で始まるコードブロック）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    for match in re.finditer(r'^-\s*```(?:python)?\s*\n(.*?)\n```', response, re.MULTILINE | re.DOTALL):
        code = match.group(1).strip()
        if _validate_candidate(code):
            candidates.append((code, 'bullet-list-code', len(code)))

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # パターン14: リスト形式（番号付きリスト内のコード）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    for match in re.finditer(r'^\d+\.\s*```(?:python)?\s*\n(.*?)\n```', response, re.MULTILINE | re.DOTALL):
        code = match.group(1).strip()
        if _validate_candidate(code):
            candidates.append((code, 'numbered-list-code', len(code)))

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # パターン15: 最後のフォールバック: 最も長い```ブロック
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    all_blocks = []
    for match in re.finditer(r'`{3,}(?:[a-z0-9]*)\s*\n(.*?)\n`{3,}', response, re.DOTALL):
        code = match.group(1).strip()
        if _validate_candidate(code):
            all_blocks.append(code)

    if all_blocks:
        longest = max(all_blocks, key=len)
        candidates.append((longest, 'fallback-longest', len(longest)))

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 結果の選択
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    if candidates:
        result, pattern, size = max(candidates, key=lambda x: x[2])
        logging.info(
            f'[Repair] コード抽出成功: パターン={pattern}, '
            f'サイズ={size} bytes, 候補数={len(candidates)}'
        )
        logging.debug(f'[Repair] 採用パターン: {pattern}')
        return result

    # テーブル応答検出（よくある失敗パターンを特定してログに残す）
    table_lines = [l for l in response.splitlines() if l.strip().startswith('|')]
    if len(table_lines) >= 3:
        logging.error(
            f'[Repair] コード抽出失敗: Claudeがテーブル形式で応答（{len(table_lines)}行のテーブル検出）。'
            f'プロンプトがコードブロック出力を強制できていない。'
            f'応答サンプル（先頭300字）:\n{response[:300]}'
        )
    else:
        logging.error(
            f'[Repair] コード抽出失敗（全15パターン試行）。'
            f'応答サンプル（先頭500字）:\n{response[:500]}'
        )
    logging.debug(f'[Repair] 完全な応答（先頭2000字）:\n{response[:2000]}')
    return None


def _verify_syntax(code: str) -> bool:
    """
    構文チェック（厳格版）
    - ast.parse() で完全構文チェック
    - SyntaxError は全て拒否（壊れたコードをファイルに書かせない）
    """
    import ast
    try:
        ast.parse(code)
        logging.debug('[Repair] 構文チェック: OK')
        return True
    except SyntaxError as e:
        error_msg = f'行{e.lineno}: {e.msg}'
        if e.text:
            error_msg += f' （{e.text.strip()}）'
        logging.error(f'[Repair] 構文エラーのため修復コードを拒否: {error_msg}')
        return False
    except Exception as e:
        exc_type = type(e).__name__
        logging.warning(f'[Repair] 構文チェック例外（許容）: {exc_type}: {str(e)[:100]}')
        return True


# ─────────────────────────────────────────────────────────────
# ログ
# ─────────────────────────────────────────────────────────────

def _log_repair(problem: Problem, success: bool, detail: str = ''):
    """修復履歴をJSONLに記録する"""
    try:
        REPAIR_LOG_PATH.parent.mkdir(exist_ok=True)
        entry = {
            'timestamp': datetime.now().isoformat(),
            'problem_type': problem.type.value,
            'affected_file': problem.affected_file,
            'severity': problem.severity,
            'success': success,
            'detail': detail,
        }
        with open(REPAIR_LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')
    except Exception as e:
        logging.error(f'[Repair] 修復ログ書き込み失敗: {e}')


# ─────────────────────────────────────────────────────────────
# 関数レベルパッチ適用（メタ修復の高速化）
# ─────────────────────────────────────────────────────────────

def _apply_function_patches(original_code: str, response: str) -> Optional[str]:
    """
    PATCH_FUNCTION形式のレスポンスから関数パッチを抽出・適用する。

    レスポンス内の期待フォーマット:
        PATCH_FUNCTION: function_name
        ```python
        def function_name(...):
            ...
        ```

    Returns: パッチ適用後のコード、パッチが見つからない場合はNone
    """
    patches: dict[str, str] = {}
    for m in re.finditer(
        r'PATCH_FUNCTION:\s*(\w+)\s*\n```(?:python)?\s*\n(.*?)\n```',
        response, re.DOTALL,
    ):
        fname = m.group(1).strip()
        code = m.group(2).strip()
        if code and len(code) > 20:
            patches[fname] = code
            logging.debug(f'[Repair] パッチ検出: {fname} ({len(code)} bytes)')

    if not patches:
        return None

    lines = original_code.splitlines(keepends=True)
    result: list[str] = []
    i = 0
    applied: list[str] = []

    # トップレベル関数の境界パターン（インデント=0）
    TOP_LEVEL_BOUNDARY = re.compile(r'^(?:def |class |# ─{10,})')

    while i < len(lines):
        m = re.match(r'^def (\w+)\s*[\(\[]', lines[i])
        if m and m.group(1) in patches:
            fname = m.group(1)
            # 関数の終端を探す（次のトップレベル定義 or EOF）
            j = i + 1
            while j < len(lines):
                if TOP_LEVEL_BOUNDARY.match(lines[j]):
                    break
                j += 1
            # パッチを挿入（末尾に空行を追加）
            result.append(patches[fname] + '\n\n')
            applied.append(fname)
            i = j
        else:
            result.append(lines[i])
            i += 1

    if not applied:
        logging.warning('[Repair] パッチ対象関数がコード中に見つかりませんでした')
        return None

    logging.info(f'[Repair] 関数パッチ適用完了: {applied}')
    return ''.join(result)


# ─────────────────────────────────────────────────────────────
# メイン修復関数（サージカルパッチ版）
# ─────────────────────────────────────────────────────────────

def _check_rate_limit() -> tuple[bool, str]:
    """
    Claude CLIのレートリミット中かどうかをファイルで確認する。
    Returns: (rate_limited: bool, message: str)
    """
    try:
        if RATE_LIMIT_FILE.exists():
            content = RATE_LIMIT_FILE.read_text(encoding='utf-8').strip()
            until = float(content)
            remaining = until - time.time()
            if remaining > 0:
                hrs = remaining / 3600
                msg = f'Claude CLIレートリミット中（残り{hrs:.1f}時間）'
                return True, msg
            RATE_LIMIT_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    return False, ''


def _set_rate_limit(duration_hours: float = 72.0):
    """レートリミットをファイルに永続化する（プロセス再起動後も有効）"""
    try:
        RATE_LIMIT_FILE.parent.mkdir(exist_ok=True)
        until = time.time() + duration_hours * 3600
        RATE_LIMIT_FILE.write_text(str(until), encoding='utf-8')
        logging.error(
            f'[Repair] レートリミット設定: {duration_hours:.0f}時間ブロック '
            f'(解除: {datetime.fromtimestamp(until).strftime("%Y-%m-%d %H:%M")})'
        )
    except Exception as e:
        logging.warning(f'[Repair] レートリミットファイル書き込み失敗: {e}')


def _detect_rate_limit(stdout: str, stderr: str) -> bool:
    """stdoutまたはstderrにレートリミットメッセージが含まれるか確認する"""
    combined = (stdout or '') + (stderr or '')
    rate_limit_patterns = [
        'hit your limit',
        'rate limit',
        'rate_limit',
        'quota exceeded',
        'too many requests',
        'try again later',
        'usage limit',
        'monthly limit',
    ]
    combined_lower = combined.lower()
    return any(p in combined_lower for p in rate_limit_patterns)


def attempt_repair(problem: Problem) -> bool:
    """
    問題を修復する（サージカルパッチ版）。
    対象関数だけをASTで抽出 → Claudeに渡す → 関数レベルで差し替え。
    ファイル全体を再生成しないため構文エラーが発生しない。

    Returns: True=成功, False=失敗またはスキップ
    """
    global _repair_count_today, _repair_count_date

    # レートリミットチェック
    rate_limited, rl_msg = _check_rate_limit()
    if rate_limited:
        logging.warning(f'[Repair] スキップ（{rl_msg}）')
        return False

    today = datetime.now().strftime('%Y-%m-%d')
    if _repair_count_date != today:
        _repair_count_date = today
        _repair_count_today = 0

    _repair_count_today += 1
    _save_repair_count()  # 再起動後も引き継ぐためファイルへ即時保存 (穴12修正)
    if _repair_count_today > MAX_REPAIRS_PER_DAY:
        logging.warning(f'[Repair] 1日の修復上限({MAX_REPAIRS_PER_DAY}回)に達しました。明日まで待機')
        return False

    # ── 対象関数を決定 ──────────────────────────────────────
    ptype_key = problem.type.value
    target_info = PROBLEM_TARGET_FUNCTIONS.get(ptype_key)
    if not target_info:
        logging.warning(f'[Repair] {ptype_key} に対応する修復関数が未定義。スキップ')
        _log_repair(problem, False, f'no target function for {ptype_key}')
        return False

    rel_file, func_name = target_info
    target_path = BASE_DIR / rel_file

    if not target_path.exists():
        logging.error(f'[Repair] 対象ファイルが見つかりません: {target_path}')
        _log_repair(problem, False, 'target file not found')
        return False

    # ── 対象関数をASTで抽出 ─────────────────────────────────
    func_source = _extract_function_source(target_path, func_name)
    if not func_source:
        logging.error(f'[Repair] 関数 {func_name} の抽出失敗')
        _log_repair(problem, False, f'function extraction failed: {func_name}')
        return False

    logging.info(
        f'[Repair] 修復開始: {problem.type.value} → {rel_file}::{func_name} '
        f'({len(func_source)}bytes, 本日{_repair_count_today}/{MAX_REPAIRS_PER_DAY}回目)'
    )

    # Claude CLI を探す
    claude_cmd, use_shell = _get_claude_cmd()
    if not claude_cmd:
        logging.error('[Repair] Claude CLIが見つかりません。修復をスキップします')
        _log_repair(problem, False, 'Claude CLI not found')
        return False

    # プロンプト構築（関数レベル・切り捨てなし）
    prompt = _build_prompt(problem, func_name, func_source)

    # バックアップ（元ファイル全体）
    _backup_file(target_path)

    # モデルとタイムアウト: 関数レベルなので小さくて済む
    if problem.severity != 'warning':
        model = 'claude-sonnet-4-6'
        base_timeout = 240  # critical: 4分
        max_attempts = 3
        timeout_scales = [1.0, 0.833, 0.667]  # [240s, 200s, 160s]
        retry_wait = [5, 10]
    else:
        model = 'claude-haiku-4-5-20251001'
        base_timeout = 180  # warning: 3分（以前の2分は短すぎた）
        max_attempts = 3
        timeout_scales = [1.0, 0.889, 0.778]  # [180s, 160s, 140s]
        retry_wait = [5, 10]

    # Claude CLI 呼び出し（最大試行回数まで再試行）
    response = None
    last_error_type = None

    for attempt in range(1, max_attempts + 1):
        try:
            # タイムアウトを段階的に短縮（MIN_TIMEOUT_SECONDS以下には絶対にしない）
            timeout_sec = max(MIN_TIMEOUT_SECONDS, int(base_timeout * timeout_scales[attempt - 1]))
            logging.info(
                f'[Repair] Claude CLI呼び出し（試行 {attempt}/{max_attempts}）: '
                f'model={model} timeout={timeout_sec}s'
            )

            # --dangerously-skip-permissions: パーミッション確認の対話を回避
            base_flags = [
                '-p',
                '--dangerously-skip-permissions',
                '--output-format', 'text',
                '--model', model
            ]

            # 環境変数: パーミッションスキップ・非対話モードを強制
            sub_env = os.environ.copy()
            sub_env['CLAUDE_SKIP_PERMISSIONS'] = '1'
            sub_env['CI'] = 'true'  # 非対話モードのヒント

            # プロンプトをtempファイルに書き出す（Windows stdin pipe不安定対策）
            prompt_tmpfile = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode='w', encoding='utf-8', suffix='.txt',
                    delete=False, prefix='claude_repair_'
                ) as tf:
                    tf.write(prompt)
                    prompt_tmpfile = tf.name
                logging.debug(f'[Repair] プロンプトtempファイル: {prompt_tmpfile} ({len(prompt)} chars)')
            except Exception as e:
                logging.warning(f'[Repair] tempファイル作成失敗（stdin fallback）: {e}')
                prompt_tmpfile = None

            try:
                if use_shell:
                    # .cmd/.bat は shell=True でないと実行できない（Windows対応）
                    quoted = f'"{claude_cmd}"' if (' ' in claude_cmd and not claude_cmd.startswith('"')) else claude_cmd
                    cli_str = f'{quoted} ' + ' '.join(base_flags)
                    if prompt_tmpfile:
                        # tempファイルからリダイレクト（stdin pipeより安定）
                        quoted_tmp = f'"{prompt_tmpfile}"'
                        cli_str_with_input = f'{cli_str} < {quoted_tmp}'
                        logging.debug(f'[Repair] CLI コマンド（shell=True, tempfile）: {cli_str[:100]}...')
                        result = subprocess.run(
                            cli_str_with_input,
                            capture_output=True,
                            text=True,
                            encoding='utf-8',
                            errors='replace',
                            timeout=timeout_sec,
                            shell=True,
                            env=sub_env,
                        )
                    else:
                        logging.debug(f'[Repair] CLI コマンド（shell=True, stdin）: {cli_str[:100]}...')
                        result = subprocess.run(
                            cli_str,
                            input=prompt,
                            capture_output=True,
                            text=True,
                            encoding='utf-8',
                            errors='replace',
                            timeout=timeout_sec,
                            shell=True,
                            env=sub_env,
                        )
                else:
                    logging.debug(f'[Repair] CLI コマンド: {claude_cmd} ...')
                    if prompt_tmpfile:
                        with open(prompt_tmpfile, 'r', encoding='utf-8') as pf:
                            result = subprocess.run(
                                [claude_cmd] + base_flags,
                                stdin=pf,
                                capture_output=True,
                                text=True,
                                encoding='utf-8',
                                errors='replace',
                                timeout=timeout_sec,
                                shell=False,
                                env=sub_env,
                            )
                    else:
                        result = subprocess.run(
                            [claude_cmd] + base_flags,
                            input=prompt,
                            capture_output=True,
                            text=True,
                            encoding='utf-8',
                            errors='replace',
                            timeout=timeout_sec,
                            shell=False,
                            env=sub_env,
                        )
            finally:
                if prompt_tmpfile:
                    try:
                        os.unlink(prompt_tmpfile)
                    except Exception:
                        pass

            if result.returncode != 0:
                error_msg = result.stderr[:500] if result.stderr else '(エラーメッセージなし)'
                stdout_snippet = (result.stdout or '')[:300]

                # レートリミット検知（stdout/stderrどちらにも現れる可能性がある）
                if _detect_rate_limit(result.stdout or '', result.stderr or ''):
                    logging.error(
                        f'[Repair] Claude CLIレートリミット検知！72時間ブロックします。'
                        f'メッセージ: {stdout_snippet or error_msg[:200]}'
                    )
                    _set_rate_limit(72.0)
                    _repair_count_today = MAX_REPAIRS_PER_DAY  # 今日の残り試行も止める
                    _log_repair(problem, False, 'rate_limit')
                    return False

                logging.error(
                    f'[Repair] Claude CLI エラー(rc={result.returncode}, 試行{attempt}/{max_attempts}): {error_msg[:200]}'
                )
                if stdout_snippet:
                    logging.error(f'[Repair] CLI stdout: {stdout_snippet}')
                logging.debug(f'[Repair] CLI stderr 完全: {error_msg}')
                last_error_type = f'CLI error rc={result.returncode}'

                if attempt < max_attempts:
                    wait_sec = retry_wait[min(attempt - 1, len(retry_wait) - 1)]
                    logging.info(f'[Repair] {wait_sec}秒待機して再試行します...')
                    time.sleep(wait_sec)
                    continue
                _log_repair(problem, False, last_error_type)
                return False

            response = result.stdout
            if not response:
                logging.error(f'[Repair] Claude CLI 応答が空（rc={result.returncode}）')
                if attempt < max_attempts:
                    wait_sec = retry_wait[min(attempt - 1, len(retry_wait) - 1)]
                    logging.info(f'[Repair] {wait_sec}秒待機して再試行します...')
                    time.sleep(wait_sec)
                    continue
                last_error_type = 'empty response'
                _log_repair(problem, False, last_error_type)
                return False

            logging.debug(f'[Repair] Claude CLI 応答取得（{len(response)} 文字）')
            break  # 成功時はループを抜ける

        except subprocess.TimeoutExpired:
            # timeout_sec はループ先頭で max(MIN_TIMEOUT_SECONDS, ...) 計算済みのため再利用
            logging.warning(
                f'[Repair] Claude CLI タイムアウト（{timeout_sec}秒、試行 {attempt}/{max_attempts}）'
            )
            last_error_type = f'timeout {timeout_sec}s'

            if attempt < max_attempts:
                wait_sec = retry_wait[min(attempt - 1, len(retry_wait) - 1)]
                next_timeout = int(base_timeout * timeout_scales[attempt])
                logging.info(
                    f'[Repair] {wait_sec}秒待機して再試行します（次: {next_timeout}s）...'
                )
                time.sleep(wait_sec)
                continue
            _log_repair(problem, False, last_error_type)
            return False

        except Exception as e:
            exc_type = type(e).__name__
            exc_msg = str(e)[:300]
            logging.error(
                f'[Repair] Claude CLI 呼び出し失敗（試行 {attempt}/{max_attempts}）: {exc_type}: {exc_msg}'
            )
            logging.debug(f'[Repair] 例外の完全メッセージ: {e}')
            last_error_type = f'exception: {exc_type}'

            if attempt < max_attempts:
                wait_sec = retry_wait[min(attempt - 1, len(retry_wait) - 1)]
                logging.info(f'[Repair] {wait_sec}秒待機して再試行します...')
                time.sleep(wait_sec)
                continue
            _log_repair(problem, False, last_error_type)
            return False
    else:
        # 全試行失敗
        logging.error('[Repair] 全ての試行が失敗しました')
        _log_repair(problem, False, 'all attempts failed')
        return False

    if not response:
        logging.error('[Repair] 応答が空です')
        _log_repair(problem, False, 'empty response')
        return False

    # コード抽出（関数コードのみ）
    new_func_code = _extract_code_from_response(response)
    if not new_func_code:
        logging.error(
            f'[Repair] 応答からコードを抽出できませんでした（応答長: {len(response)} 文字）'
        )
        logging.warning(f'[Repair] Claude応答（先頭500字）:\n{response[:500]}')
        _log_repair(problem, False, 'code extraction failed')
        return False

    # 構文チェック
    if not _verify_syntax(new_func_code):
        logging.error('[Repair] 修復コードに構文エラー。適用スキップ')
        _log_repair(problem, False, 'syntax error')
        return False

    # 関数定義チェック: 正しい関数名が含まれているか
    if f'def {func_name}' not in new_func_code:
        logging.error(
            f'[Repair] 応答に "def {func_name}" が含まれていません。適用スキップ'
        )
        logging.warning(f'[Repair] Claude応答（先頭500字）:\n{response[:500]}')
        _log_repair(problem, False, f'missing def {func_name}')
        return False

    # サイズチェック（関数レベル: 元関数との比較）
    orig_func_len = len(func_source)
    new_func_len = len(new_func_code)
    func_ratio = new_func_len / orig_func_len if orig_func_len > 0 else 1.0
    logging.info(
        f'[Repair] 関数サイズチェック: {new_func_len}/{orig_func_len} bytes '
        f'(ratio={func_ratio:.1%})'
    )

    # 関数が短くなった場合は拒否（元の80%未満）(穴1追加修正)
    # Claudeが重要処理を削除して短縮版を返す事故を防ぐため閾値を厳格化
    if func_ratio < 0.80:
        logging.error(
            f'[Repair] 修復関数が短すぎる（{func_ratio:.1%}）。機能削除の可能性があると判定。スキップ'
        )
        _log_repair(problem, False, f'function too short (ratio={func_ratio:.1%}, threshold=80%)')
        return False

    # サージカルパッチ適用: 対象関数だけを差し替え（ファイル全体は変更しない）
    try:
        success = _replace_function_in_file(target_path, func_name, new_func_code)
    except Exception as e:
        logging.error(f'[Repair] サージカルパッチ適用失敗: {e}')
        _log_repair(problem, False, f'patch apply error: {str(e)[:100]}')
        return False

    if success:
        logging.info(
            f'[Repair] ✓修復完了: {problem.affected_file}::{func_name} '
            f'(本日{_repair_count_today}/{MAX_REPAIRS_PER_DAY}回目, '
            f'{orig_func_len}→{new_func_len} bytes, ratio={func_ratio:.1%})'
        )
        _log_repair(problem, True, f'success (ratio={func_ratio:.1%})')
        return True
    else:
        logging.error(
            f'[Repair] _replace_function_in_file が False を返しました '
            f'（関数 {func_name} がファイル内に見つからない可能性）'
        )
        _log_repair(problem, False, f'function not found in file: {func_name}')
        return False
