import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse


MVN_LOCK = threading.Lock()
NS = {"m": "http://maven.apache.org/POM/4.0.0"}

DEPENDENCY_PLUGIN = "org.apache.maven.plugins:maven-dependency-plugin:3.8.1:tree"
STUB_MARKER = "[AUTO-GENERATED STUB]"


class DepNode:
    __slots__ = ("group", "artifact", "version", "scope", "optional",
                 "depth", "parent", "module")

    def __init__(self, group: str, artifact: str, version: str,
                 scope: str = "", optional: bool = False,
                 depth: int = 0, parent: str = "", module: str = ""):
        self.group = group
        self.artifact = artifact
        self.version = version
        self.scope = scope
        self.optional = optional
        self.depth = depth
        self.parent = parent
        self.module = module

    def gav(self) -> str:
        return f"{self.group}:{self.artifact}:{self.version}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "group": self.group,
            "artifact": self.artifact,
            "version": self.version,
            "scope": self.scope or None,
            "optional": self.optional,
            "depth": self.depth,
            "parent": self.parent or None,
            "module": self.module or None,
            "is_transitive": self.depth > 1,
            "purl": f"pkg:maven/{self.group}/{self.artifact}@{self.version}",
        }


# ========== 本地 Maven 仓库候选目录 ==========
def resolve_m2_repos() -> List[Path]:
    candidates: List[Path] = []

    multi = os.environ.get("M2_REPOS", "").strip()
    if multi:
        sep = ";" if sys.platform == "win32" else ":"
        for p in multi.split(sep):
            p = p.strip().strip('"').strip("'")
            if p:
                candidates.append(Path(p))

    single = os.environ.get("M2_REPO", "").strip().strip('"').strip("'")
    if single:
        candidates.append(Path(single))

    candidates.append(Path.home() / ".m2" / "repository")

    if sys.platform == "win32":
        candidates.extend([
            Path(r"Z:\repo5"),
            Path(r"Z:\repo"),
            Path(r"D:\repo5"),
            Path(r"D:\m2repo"),
        ])

    seen = set()
    result: List[Path] = []
    for c in candidates:
        try:
            if not c.exists() or not c.is_dir():
                continue
            key = str(c.resolve()).lower() if sys.platform == "win32" else str(c.resolve())
            if key in seen:
                continue
            seen.add(key)
            result.append(c)
        except OSError:
            continue
    return result


def _get_primary_repo() -> Path:
    """获取当前 Maven 实际使用的本地仓库根目录"""
    explicit = os.environ.get("M2_REPO", "").strip().strip('"').strip("'")
    if explicit:
        p = Path(explicit)
        if p.exists():
            return p
    multi = os.environ.get("M2_REPOS", "").strip()
    if multi:
        sep = ";" if sys.platform == "win32" else ":"
        for part in multi.split(sep):
            part = part.strip().strip('"').strip("'")
            if part and Path(part).exists():
                return Path(part)
    return Path.home() / ".m2" / "repository"


# ========== 本地 Maven 仓库 jar 的 sha256 计算 ==========
def compute_jar_sha256(group: str, artifact: str, version: str,
                       m2_root: Optional[Path] = None) -> str:
    if not group or not artifact or not version:
        return ""
    try:
        repos = [Path(m2_root)] if m2_root is not None else resolve_m2_repos()
        if not repos:
            return ""
        rel = Path(group.replace(".", "/")) / artifact / version / f"{artifact}-{version}.jar"
        for repo in repos:
            jar = repo / rel
            if jar.exists():
                h = hashlib.sha256()
                with open(jar, "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        h.update(chunk)
                return f"sha256:{h.hexdigest()}"
        return ""
    except Exception:
        return ""


# ========== Maven 可执行文件解析 ==========
def resolve_maven(project_dir: Optional[Path] = None) -> str:
    env_bin = os.environ.get("MAVEN_BIN", "").strip()
    if env_bin:
        p = Path(env_bin)
        if p.exists():
            return str(p)
        if sys.platform == "win32" and not env_bin.lower().endswith((".cmd", ".bat", ".exe")):
            for ext in (".cmd", ".bat"):
                if Path(env_bin + ext).exists():
                    return env_bin + ext

    use_wrapper = os.environ.get("MAVEN_WRAPPER_DISABLE", "true").strip().lower() != "true"
    if use_wrapper and project_dir:
        for name in ("mvnw.cmd", "mvnw"):
            p = project_dir / name
            if p.exists():
                return str(p)

    if sys.platform == "win32":
        for name in ("mvn.cmd", "mvn.bat", "mvn.exe", "mvn"):
            found = shutil.which(name)
            if found:
                return found
    else:
        found = shutil.which("mvn")
        if found:
            return found

    if sys.platform == "win32":
        patterns = [
            r"C:\Program Files\apache-maven-*\bin\mvn.cmd",
            r"C:\Program Files (x86)\apache-maven-*\bin\mvn.cmd",
            r"C:\tools\apache-maven-*\bin\mvn.cmd",
            r"C:\apache-maven-*\bin\mvn.cmd",
            os.path.expandvars(r"%USERPROFILE%\apache-maven-*\bin\mvn.cmd"),
            os.path.expandvars(r"%USERPROFILE%\scoop\apps\maven\current\bin\mvn.cmd"),
        ]
    else:
        patterns = [
            "/usr/share/maven/bin/mvn",
            "/usr/local/maven/bin/mvn",
            "/opt/maven/bin/mvn",
            "/opt/apache-maven-*/bin/mvn",
        ]
    for pattern in patterns:
        matches = glob.glob(pattern)
        if matches:
            return matches[0]

    return ""


# ========== Git 可执行文件解析 ==========
def resolve_git() -> str:
    env_bin = os.environ.get("GIT_BIN", "").strip()
    if env_bin:
        p = Path(env_bin)
        if p.exists():
            return str(p)
        if sys.platform == "win32" and not env_bin.lower().endswith(".exe"):
            if Path(env_bin + ".exe").exists():
                return env_bin + ".exe"

    if sys.platform == "win32":
        for name in ("git.exe", "git.cmd", "git"):
            found = shutil.which(name)
            if found:
                return found
    else:
        found = shutil.which("git")
        if found:
            return found

    if sys.platform == "win32":
        patterns = [
            r"C:\Program Files\Git\cmd\git.exe",
            r"C:\Program Files\Git\bin\git.exe",
            r"C:\Program Files (x86)\Git\cmd\git.exe",
            r"C:\Program Files (x86)\Git\bin\git.exe",
            r"C:\Git\cmd\git.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Git\cmd\git.exe"),
            os.path.expandvars(r"%USERPROFILE%\scoop\apps\git\current\cmd\git.exe"),
            os.path.expandvars(r"%USERPROFILE%\scoop\shims\git.exe"),
        ]
    else:
        patterns = [
            "/usr/bin/git",
            "/usr/local/bin/git",
            "/opt/homebrew/bin/git",
        ]
    for pattern in patterns:
        if Path(pattern).exists():
            return pattern
    return ""


# ========== 构建系统识别与根 POM 定位 ==========
def detect_build_system(project_dir: Path) -> Optional[str]:
    if any(project_dir.rglob("pom.xml")):
        return "maven"
    if (project_dir / "build.gradle").exists() or (project_dir / "build.gradle.kts").exists():
        return "gradle"
    return None


def find_root_pom(project_dir: Path) -> Optional[Path]:
    poms = list(project_dir.rglob("pom.xml"))
    if not poms:
        return None
    if len(poms) == 1:
        return poms[0]

    module_poms = set()
    for pom in poms:
        try:
            tree = ET.parse(pom)
            for mod in tree.getroot().findall(".//m:modules/m:module", NS):
                rel = (mod.text or "").strip()
                if not rel:
                    continue
                mod_path = (pom.parent / rel).resolve()
                if mod_path.is_dir():
                    mod_path = mod_path / "pom.xml"
                if mod_path.exists():
                    module_poms.add(mod_path.resolve())
        except ET.ParseError:
            continue

    roots = [p.resolve() for p in poms if p.resolve() not in module_poms]
    return roots[0] if roots else poms[0]


# ========== 从 Maven 输出中提取缺失依赖 GAV ==========
def _extract_missing_gavs(mvn_output: str) -> List[Tuple[str, str, str]]:
    """
    从 Maven 输出中提取所有无法解析的普通依赖 GAV。
    只提取 artifact descriptor / transfer / resolution 失败，不提取 Non-resolvable parent POM
    （父 POM 的 stub 会导致子项目继承的属性全部丢失，反而更糟，交给降级解析兜底）。
    """
    patterns = [
        r"Failed to read artifact descriptor for\s+([\w.\-]+):([\w.\-]+):(?:[\w\-]+):([\w.\-]+)",
        r"Could not transfer artifact\s+([\w.\-]+):([\w.\-]+):(?:[\w\-]+):([\w.\-]+)",
        r"The following artifacts could not be resolved:.*?\n\s*([\w.\-]+):([\w.\-]+):(?:[\w\-]+):([\w.\-]+)",
        r"Could not find artifact\s+([\w.\-]+):([\w.\-]+):(?:[\w\-]+):([\w.\-]+)",
        r"Could not resolve dependencies for project.*?Failed to collect dependencies at\s+([\w.\-]+):([\w.\-]+):(?:[\w\-]+):([\w.\-]+)",
    ]
    results: List[Tuple[str, str, str]] = []
    seen = set()
    for pat in patterns:
        for m in re.finditer(pat, mvn_output, flags=re.DOTALL):
            gav = (m.group(1), m.group(2), m.group(3))
            if gav not in seen:
                seen.add(gav)
                results.append(gav)
    return results


# ========== 写入 stub POM（这就是"跳过"的核心） ==========
def _write_stub_pom(repo_root: Path, group: str, artifact: str, version: str) -> Optional[Path]:
    """
    在本地仓库写入一个 stub POM，让 Maven 认为该构件"存在"，
    从而在 dependency:tree 中跳过它、继续解析其他依赖。
    如果目标位置已有真实 POM（不含 STUB_MARKER），不覆盖。
    """
    if not group or not artifact or not version:
        return None
    rel = Path(group.replace(".", "/")) / artifact / version
    target_dir = repo_root / rel
    pom_path = target_dir / f"{artifact}-{version}.pom"

    if pom_path.exists():
        try:
            existing = pom_path.read_text(encoding="utf-8", errors="ignore")
            if STUB_MARKER not in existing:
                return None  # 已有真实 POM，跳过
        except OSError:
            return None

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None

    content = f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 http://maven.apache.org/xsd/maven-4.0.0.xsd">
    <modelVersion>4.0.0</modelVersion>
    <groupId>{group}</groupId>
    <artifactId>{artifact}</artifactId>
    <version>{version}</version>
    <packaging>jar</packaging>
    <description>{STUB_MARKER} POM unavailable from remote; placeholder created by governance tool.</description>
</project>
"""
    try:
        pom_path.write_text(content, encoding="utf-8")
    except OSError:
        return None

    # 清除同目录下的失败标记元数据（否则 Maven 仍会用缓存中的失败记录）
    for meta in target_dir.glob("*.lastUpdated"):
        try:
            meta.unlink()
        except OSError:
            pass
    for meta in target_dir.glob("_remote.repositories"):
        try:
            meta.unlink()
        except OSError:
            pass

    return pom_path


# ========== 纯 XML 降级解析（增强版） ==========
def _get_child_text(elem, tag: str) -> str:
    for path, ns in [(f"m:{tag}", NS), (tag, None)]:
        try:
            found = elem.find(path, ns) if ns else elem.find(path)
        except Exception:
            found = None
        if found is not None and found.text:
            return found.text.strip()
    return ""


def _collect_pom_properties(root) -> Dict[str, str]:
    properties: Dict[str, str] = {}
    for prop in root.findall(".//m:properties/*", NS):
        tag = prop.tag.split("}")[-1] if "}" in prop.tag else prop.tag
        if prop.text:
            properties[tag] = prop.text.strip()
    return properties


def _resolve_placeholder(value: str, properties: Dict[str, str]) -> str:
    if not value or "${" not in value:
        return value
    return re.sub(
        r'\$\{([^}]+)\}',
        lambda m: properties.get(m.group(1), m.group(0)),
        value,
    )


def _collect_dep_elements(root) -> List:
    elements = []
    deps = root.findall("m:dependencies/m:dependency", NS)
    if not deps:
        deps = root.findall("dependencies/dependency")
    elements.extend(deps)
    elements.extend(root.findall("m:profiles/m:profile/m:dependencies/m:dependency", NS))
    elements.extend(root.findall("profiles/profile/dependencies/dependency"))
    return elements


def _collect_dm_elements(root) -> List:
    elements = root.findall("m:dependencyManagement/m:dependencies/m:dependency", NS)
    if not elements:
        elements = root.findall("dependencyManagement/dependencies/dependency")
    return elements


def parse_pom_dependencies(project_dir: Path) -> Tuple[List[DepNode], List[str]]:
    """
    降级解析：直接读 pom.xml，不调用 Maven。
    只取 <project>/<dependencies> 和 <profiles>/*/<dependencies>；
    若都没有，退到 <dependencyManagement> 兜底（warning 里会标注）。
    """
    nodes: List[DepNode] = []
    warnings: List[str] = []
    seen_gav = set()
    dm_fallback: List[Tuple[str, str, str, str, str]] = []

    pom_files = [p for p in project_dir.rglob("pom.xml") if "target" not in p.parts]
    if not pom_files:
        return [], ["降级解析：未找到 pom.xml"]

    for pom_path in pom_files:
        try:
            tree = ET.parse(pom_path)
            root = tree.getroot()
        except ET.ParseError as e:
            warnings.append(f"降级解析：{pom_path} 解析失败: {e}")
            continue

        properties = _collect_pom_properties(root)

        try:
            module_name = str(pom_path.parent.relative_to(project_dir)) or "."
        except ValueError:
            module_name = pom_path.parent.name

        for dep in _collect_dep_elements(root):
            g = _resolve_placeholder(_get_child_text(dep, "groupId"), properties)
            a = _resolve_placeholder(_get_child_text(dep, "artifactId"), properties)
            v = _resolve_placeholder(_get_child_text(dep, "version"), properties)
            s = _resolve_placeholder(_get_child_text(dep, "scope"), properties)
            if not g or not a:
                continue
            key = f"{g}:{a}:{v}"
            if key in seen_gav:
                continue
            seen_gav.add(key)
            nodes.append(DepNode(
                group=g, artifact=a, version=v, scope=s,
                depth=1, parent="", module=module_name,
            ))

        for dep in _collect_dm_elements(root):
            g = _resolve_placeholder(_get_child_text(dep, "groupId"), properties)
            a = _resolve_placeholder(_get_child_text(dep, "artifactId"), properties)
            v = _resolve_placeholder(_get_child_text(dep, "version"), properties)
            s = _resolve_placeholder(_get_child_text(dep, "scope"), properties)
            if g and a:
                dm_fallback.append((g, a, v, s, module_name))

    if not nodes and dm_fallback:
        warnings.append(
            f"降级解析：项目 <dependencies> 为空，改用 <dependencyManagement> 中的 "
            f"{len(dm_fallback)} 个依赖作为兜底（这些依赖可能未实际被使用）"
        )
        for g, a, v, s, module_name in dm_fallback:
            key = f"{g}:{a}:{v}"
            if key in seen_gav:
                continue
            seen_gav.add(key)
            nodes.append(DepNode(
                group=g, artifact=a, version=v, scope=s,
                depth=1, parent="", module=module_name,
            ))

    return nodes, warnings


# ========== Maven 解析（stub 跳过 + 降级兜底） ==========
def collect_maven(project_dir: Path, timeout: int = 600
                  ) -> Tuple[List[DepNode], List[str], List[Dict[str, Any]]]:
    """
    返回 (nodes, warnings, skipped_dependencies)。
    skipped_dependencies 是 List[Dict]，记录每个被 stub 跳过的依赖。
    """
    root_pom = find_root_pom(project_dir)
    if not root_pom:
        return [], ["未找到 pom.xml"], []

    mvn_bin = resolve_maven(project_dir)
    if not mvn_bin:
        return [], [
            "未找到 mvn 命令。请设置环境变量 MAVEN_BIN 指向 mvn.cmd 的完整路径。"
        ], []

    warnings: List[str] = []
    settings = os.environ.get("MAVEN_SETTINGS_XML")

    for pom in project_dir.rglob("pom.xml"):
        try:
            (pom.parent / "target").mkdir(parents=True, exist_ok=True)
        except OSError as e:
            warnings.append(f"无法创建 {pom.parent / 'target'}: {e}")

    cmd = [
        mvn_bin, "-f", str(root_pom), DEPENDENCY_PLUGIN,
        "-DoutputType=json",
        "-DoutputFile=${project.basedir}/target/dependency-tree.json",
        "-Dverbose=false",
        "-Dmaven.test.skip=true",
        "--batch-mode",
        "--fail-at-end",
    ]
    if settings and Path(settings).exists():
        cmd += ["-s", settings]

    explicit_repo = os.environ.get("M2_REPO", "").strip().strip('"').strip("'")
    if explicit_repo:
        cmd.append(f"-Dmaven.repo.local={explicit_repo}")

    force_update = os.environ.get("MAVEN_FORCE_UPDATE", "true").strip().lower() != "false"
    if force_update:
        cmd.append("-U")

    # stub 重试配置
    stub_enabled = os.environ.get("MAVEN_STUB_RETRY", "true").strip().lower() != "false"
    try:
        max_rounds = int(os.environ.get("MAVEN_STUB_MAX_RETRIES", "3"))
    except ValueError:
        max_rounds = 3
    if max_rounds < 0:
        max_rounds = 0

    mvn_output = ""
    mvn_returncode = 0
    tree_files: List[Path] = []
    stubbed_gavs: List[Tuple[str, str, str]] = []
    seen_stubbed: set = set()

    # 循环重试：失败 -> 提取缺失 GAV -> 写 stub POM（跳过） -> 重试
    for attempt in range(max_rounds + 1):
        with MVN_LOCK:
            try:
                proc = subprocess.run(
                    cmd, cwd=str(root_pom.parent),
                    capture_output=True, text=True, timeout=timeout,
                )
                mvn_output = (proc.stdout or "") + "\n" + (proc.stderr or "")
                mvn_returncode = proc.returncode
            except subprocess.TimeoutExpired:
                return [], [f"Maven 执行超时（>{timeout}s）"], []
            except (FileNotFoundError, OSError) as e:
                return [], [f"无法执行 Maven: {mvn_bin} ({e})"], []

        tree_files = list(project_dir.rglob("target/dependency-tree.json"))
        if tree_files:
            break

        if mvn_returncode == 0:
            break

        if not stub_enabled or attempt >= max_rounds:
            break

        missing_gavs = _extract_missing_gavs(mvn_output)
        new_missing = [g for g in missing_gavs if g not in seen_stubbed]
        if not new_missing:
            break

        repo_root = _get_primary_repo()
        created_this_round: List[Tuple[str, str, str]] = []
        for g, a, v in new_missing:
            try:
                pom_path = _write_stub_pom(repo_root, g, a, v)
                if pom_path:
                    created_this_round.append((g, a, v))
                    seen_stubbed.add((g, a, v))
            except Exception as e:
                warnings.append(f"写入 stub POM 失败 {g}:{a}:{v}: {e}")

        if not created_this_round:
            break

        stubbed_gavs.extend(created_this_round)
        preview = ", ".join(f"{g}:{a}:{v}" for g, a, v in created_this_round[:5])
        more = "..." if len(created_this_round) > 5 else ""
        warnings.append(
            f"第 {attempt + 1} 轮：跳过 {len(created_this_round)} 个无法下载的依赖 "
            f"（{preview}{more}），已写入 stub POM，重试 Maven"
        )

    if mvn_returncode != 0:
        warnings.append(f"Maven 返回码 {mvn_returncode}: {mvn_output[-1200:]}")

    skipped_info: List[Dict[str, Any]] = [
        {
            "group": g,
            "artifact": a,
            "version": v,
            "reason": "unresolvable_from_remote",
            "action": "stub_pom_created_and_skipped",
            "purl": f"pkg:maven/{g}/{a}@{v}",
        }
        for g, a, v in stubbed_gavs
    ]

    if not tree_files:
        snippet = mvn_output[-1200:] if mvn_output else "(无输出)"
        warnings.append(f"未生成 dependency-tree.json。Maven 输出尾部: {snippet}")

        fallback_enabled = (
            os.environ.get("MAVEN_FALLBACK_ON_FAILURE", "true").strip().lower() != "false"
        )
        if fallback_enabled:
            warnings.append(
                "已降级为纯 POM XML 解析：仅能获取各模块声明的直接依赖，"
                "不含传递依赖；含 ${xxx} 占位符的版本号会保留原样。"
            )
            fb_nodes, fb_warnings = parse_pom_dependencies(project_dir)
            warnings.extend(fb_warnings)
            if fb_nodes:
                warnings.append(f"降级解析获取到 {len(fb_nodes)} 个直接依赖")
                return fb_nodes, warnings, skipped_info
            warnings.append("降级解析未获取到任何依赖")
        return [], warnings, skipped_info

    nodes: List[DepNode] = []
    for tf in tree_files:
        try:
            with open(tf, "r", encoding="utf-8") as f:
                data = json.load(f)
            try:
                module_name = str(tf.parent.parent.relative_to(project_dir))
            except ValueError:
                module_name = tf.parent.parent.name
            flatten_maven_tree(data, module=module_name, result=nodes)
        except Exception as e:
            warnings.append(f"解析 {tf} 失败: {e}")

    # 对于被 stub 的依赖，如果它在 dependency-tree.json 里出现过但被跳过，
    # 不在 nodes 里出现是正常的（Maven 直接跳过该子树）
    return nodes, warnings, skipped_info


def flatten_maven_tree(node: Dict, depth: int = 0, parent: str = "",
                       module: str = "", result: Optional[List[DepNode]] = None) -> List[DepNode]:
    if result is None:
        result = []

    group = node.get("groupId", "")
    artifact = node.get("artifactId", "")
    version = node.get("version", "")
    scope = node.get("scope", "") or ""
    optional = str(node.get("optional", "false")).lower() == "true"
    coord = f"{group}:{artifact}:{version}"

    if depth > 0 and group and artifact:
        result.append(DepNode(group, artifact, version, scope, optional,
                              depth, parent, module))

    for child in node.get("children", []) or []:
        flatten_maven_tree(child, depth + 1, coord, module, result)

    return result


# ========== Gradle 解析 ==========
def collect_gradle(project_dir: Path, timeout: int = 600
                   ) -> Tuple[List[DepNode], List[str], List[Dict[str, Any]]]:
    warnings: List[str] = []
    gradlew = project_dir / "gradlew"
    cmd = ["./gradlew", "dependencies", "--configuration", "runtimeClasspath"] \
        if gradlew.exists() else ["gradle", "dependencies", "--configuration", "runtimeClasspath"]

    try:
        proc = subprocess.run(cmd, cwd=str(project_dir),
                              capture_output=True, text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return [], [f"Gradle 执行失败: {e}"], []

    nodes: List[DepNode] = []
    parent_stack: List[Tuple[int, str]] = []
    current_section = ""

    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if " - " in stripped and not stripped.startswith(("+---", "\\---", "|")):
            current_section = stripped
            parent_stack = []
            continue

        m = re.match(r"^([|\\+\\- \t]*)[+\\\\]---\s+(.+)$", line)
        if not m:
            continue
        depth = m.group(1).count("|") + m.group(1).count("    ") // 4
        parts = m.group(2).strip().split(":")
        if len(parts) < 2:
            continue
        group, artifact = parts[0], parts[1]
        version = parts[2].split()[0] if len(parts) > 2 else ""

        while parent_stack and parent_stack[-1][0] >= depth:
            parent_stack.pop()
        parent = parent_stack[-1][1] if parent_stack else ""
        coord = f"{group}:{artifact}:{version}"
        parent_stack.append((depth, coord))

        nodes.append(DepNode(group, artifact, version,
                             scope=current_section, depth=depth, parent=parent))

    return nodes, warnings, []


# ========== 去重 ==========
def deduplicate_nodes(nodes: List[DepNode]) -> List[DepNode]:
    best: Dict[str, DepNode] = {}
    for n in nodes:
        gav = n.gav()
        if gav not in best or n.depth < best[gav].depth:
            best[gav] = n
    return list(best.values())


# ========== GitLab 克隆 ==========
def clone_gitlab(repo_url: str, token: Optional[str], branch: Optional[str],
                 target_dir: Path, timeout: int = 300) -> None:
    if not repo_url.startswith(("https://", "http://")):
        raise ValueError("仅支持 http(s) 协议")

    allowed = os.environ.get("GITLAB_ALLOWED_HOSTS", "").strip()
    if allowed:
        host = urlparse(repo_url).hostname or ""
        whitelist = [h.strip() for h in allowed.split(",") if h.strip()]
        if host not in whitelist:
            raise ValueError(f"不允许的 GitLab 主机: {host}")

    git_bin = resolve_git()
    if not git_bin:
        raise RuntimeError(
            "未找到 git 命令。请安装 Git 并确保服务进程 PATH 包含 git，"
            "或设置环境变量 GIT_BIN 指向 git.exe 的完整路径。"
        )

    parsed = urlparse(repo_url)
    if token:
        netloc = f"oauth2:{urllib.parse.quote(token, safe='')}@{parsed.netloc}"
        clone_url = f"{parsed.scheme}://{netloc}{parsed.path}"
    else:
        clone_url = repo_url

    cmd = [git_bin, "clone", "--depth", "1"]
    if branch:
        cmd += ["--branch", branch]
    cmd += [clone_url, str(target_dir)]

    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise RuntimeError("git clone 超时")
    except (FileNotFoundError, OSError) as e:
        raise RuntimeError(f"无法执行 git: {git_bin} ({e})")

    if proc.returncode != 0:
        err = proc.stderr or ""
        if token:
            err = err.replace(token, "***")
        raise RuntimeError(f"git clone 失败: {err[-800:]}")
