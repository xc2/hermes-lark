"""Offline acceptance contract for the OpenClaw Lark migration."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import re
import sys
import tomllib
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = ROOT / "tests" / "fixtures" / "openclaw_lark_parity.json"
TOOL_INVENTORY_PATH = ROOT / "hermes_lark" / "data" / "openclaw-tools.json"


def _read(relative_path: str) -> str:
    """Read a UTF-8 repository file."""
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _parse(relative_path: str) -> ast.Module:
    """Parse a repository Python module without importing it."""
    return ast.parse(_read(relative_path), filename=relative_path)


def _fixture() -> dict[str, Any]:
    """Load the independently pinned upstream parity fixture."""
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert fixture.get("format_version") == 3
    return fixture


def _manifest_scalar(source: str, key: str) -> str | None:
    """Return one top-level scalar from the plugin YAML subset."""
    match = re.search(rf"(?m)^{re.escape(key)}:\s*([^>|\n].*)$", source)
    if match is None:
        return None
    return match.group(1).strip().strip("\"'")


def _manifest_env_names(source: str, section: str) -> list[str]:
    """Collect environment-variable names from a manifest list."""
    lines = source.splitlines()
    try:
        start = next(index for index, line in enumerate(lines) if line == f"{section}:")
    except StopIteration:
        return []

    names: list[str] = []
    for line in lines[start + 1 :]:
        if line and not line[0].isspace():
            break
        match = re.match(r"\s+-\s+name:\s*([A-Z][A-Z0-9_]*)\s*$", line)
        if match is not None:
            names.append(match.group(1))
    return names


def _function(
    module: ast.Module,
    name: str,
) -> ast.FunctionDef | ast.AsyncFunctionDef:
    """Find one function or method by name."""
    matches = [
        node
        for node in ast.walk(module)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    ]
    assert len(matches) == 1, f"expected exactly one {name}()"
    return matches[0]


def _call_name(call: ast.Call) -> str | None:
    """Return the final identifier of a call target."""
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _literal(node: ast.AST) -> Any:
    """Evaluate a literal or simple integer-arithmetic AST expression."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_literal(node.operand)
    if isinstance(node, ast.BinOp):
        left = _literal(node.left)
        right = _literal(node.right)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.FloorDiv):
            return left // right
    raise ValueError(f"not a supported static literal: {ast.dump(node)}")


def _module_constants(module: ast.Module) -> dict[str, Any]:
    """Collect statically evaluable top-level assignments."""
    values: dict[str, Any] = {}
    for node in module.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value_node = node.value
        if value_node is None:
            continue
        try:
            value = _literal(value_node)
        except (TypeError, ValueError):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                values[target.id] = value
    return values


def _getenv_defaults(module: ast.Module) -> dict[str, set[Any]]:
    """Collect literal defaults passed to environment-setting resolvers."""
    defaults: dict[str, set[Any]] = {}
    for node in ast.walk(module):
        if not isinstance(node, ast.Call):
            continue
        env_node: ast.AST | None = None
        default_node: ast.AST | None = None
        if (
            len(node.args) >= 2
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "getenv"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "os"
        ):
            env_node = node.args[0]
            default_node = node.args[1]
        elif isinstance(node.func, ast.Name) and node.func.id == "setting":
            keywords = {
                keyword.arg: keyword.value
                for keyword in node.keywords
                if keyword.arg is not None
            }
            env_node = keywords.get("env_name")
            default_node = keywords.get("default")
        if env_node is None or default_node is None:
            continue
        try:
            env_name = _literal(env_node)
            default = _literal(default_node)
        except (TypeError, ValueError):
            continue
        if isinstance(env_name, str):
            defaults.setdefault(env_name, set()).add(default)
    return defaults


def _frontmatter(skill_path: Path) -> dict[str, str]:
    """Parse the scalar fields needed from one skill frontmatter block."""
    lines = skill_path.read_text(encoding="utf-8").splitlines()
    assert lines and lines[0] == "---", f"{skill_path} has no frontmatter"
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise AssertionError(f"{skill_path} has unterminated frontmatter") from exc

    values: dict[str, str] = {}
    for index, line in enumerate(lines[1:end], start=1):
        match = re.match(r"^([A-Za-z][A-Za-z0-9_-]*):\s*(.*)$", line)
        if match is None:
            continue
        key, value = match.groups()
        if value in {"|", ">"}:
            continuation = [
                item.strip()
                for item in lines[index + 1 : end]
                if item.startswith((" ", "\t")) and item.strip()
            ]
            value = "\n".join(continuation)
        values[key] = value.strip().strip("\"'")
    return values


def _without_descriptions(value: Any) -> Any:
    """Remove localizable descriptions from a JSON-compatible value."""
    if isinstance(value, dict):
        return {
            key: _without_descriptions(item)
            for key, item in value.items()
            if not (key == "description" and isinstance(item, str))
        }
    if isinstance(value, list):
        return [_without_descriptions(item) for item in value]
    return value


def _schema_hash(tool: dict[str, Any]) -> str:
    """Hash the non-localized OpenClaw tool contract canonically."""
    contract = {
        "name": tool["name"],
        "parameters": _without_descriptions(tool["parameters"]),
    }
    canonical = json.dumps(
        contract,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _description_annotations(value: Any) -> list[str]:
    """Collect JSON Schema description annotations without field-name confusion."""
    annotations: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "description" and isinstance(item, str):
                annotations.append(item)
            else:
                annotations.extend(_description_annotations(item))
    elif isinstance(value, list):
        for item in value:
            annotations.extend(_description_annotations(item))
    return annotations


def _file_hash(path: Path) -> str:
    """Hash a fixture-backed file without normalizing its contents."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _check_plugin_manifest_covers_adapter_configuration() -> None:
    """The Hermes manifest exposes every parity-critical adapter setting."""
    expected = _fixture()["plugin_manifest"]
    source = _read("plugin.yaml")

    assert _manifest_scalar(source, "name") == expected["name"]
    assert _manifest_scalar(source, "kind") == expected["kind"]
    assert _manifest_scalar(source, "label")

    required = _manifest_env_names(source, "requires_env")
    optional = _manifest_env_names(source, "optional_env")
    assert len(required) == len(set(required)), "duplicate requires_env entry"
    assert len(optional) == len(set(optional)), "duplicate optional_env entry"
    assert not set(required) & set(optional), "env cannot be both required and optional"
    expected_required = set(expected["required_env"])
    missing_required = expected_required - set(required)
    unexpected_required = set(required) - expected_required
    missing_optional = set(expected["optional_env"]) - set(optional)
    assert not missing_required, f"missing required manifest env: {sorted(missing_required)}"
    assert not unexpected_required, (
        f"unexpected mandatory manifest env: {sorted(unexpected_required)}"
    )
    assert not missing_optional, f"missing optional manifest env: {sorted(missing_optional)}"
    unsupported_transport_env = {
        "FEISHU_ENCRYPT_KEY",
        "FEISHU_VERIFICATION_TOKEN",
        "FEISHU_WEBHOOK_HOST",
        "FEISHU_WEBHOOK_PATH",
        "FEISHU_WEBHOOK_PORT",
    }
    assert not unsupported_transport_env & set(optional), (
        "the public manifest must remain WebSocket-only"
    )


def _check_adapter_registers_the_complete_platform_surface() -> None:
    """The copied adapter remains reachable through Hermes plugin hooks."""
    register = _function(_parse("hermes_lark/adapter.py"), "register")
    calls = [
        node
        for node in ast.walk(register)
        if isinstance(node, ast.Call) and _call_name(node) == "register_platform"
    ]
    assert len(calls) == 1

    call = calls[0]
    keywords = {item.arg: item.value for item in call.keywords if item.arg is not None}
    assert _literal(keywords["name"]) == "feishu"
    assert {
        "adapter_factory",
        "check_fn",
        "is_connected",
        "validate_config",
        "setup_fn",
        "apply_yaml_config_fn",
        "allowed_users_env",
        "allow_all_env",
        "cron_deliver_env_var",
        "standalone_sender_fn",
        "max_message_length",
    } <= keywords.keys()


def _check_all_upstream_tool_schemas_are_preserved() -> None:
    """All 39 pinned schemas retain their non-localized contract."""
    expected_hashes = _fixture()["tools"]["structural_schema_sha256"]
    inventory = json.loads(TOOL_INVENTORY_PATH.read_text(encoding="utf-8"))
    tools = inventory["tools"]

    assert inventory["channels"] == ["feishu"]
    assert len(expected_hashes) == 39
    assert [tool["name"] for tool in tools] == list(expected_hashes)
    assert len(tools) == len({tool["name"] for tool in tools})

    actual_hashes: dict[str, str] = {}
    for tool in tools:
        assert tool["label"].strip()
        assert tool["description"].strip()
        if "opts" in tool:
            assert tool["opts"]["name"] == tool["name"]
        parameters = tool["parameters"]
        assert parameters.get("type") == "object" or "anyOf" in parameters
        annotations = _description_annotations(parameters)
        assert all(annotation.strip() for annotation in annotations)
        actual_hashes[tool["name"]] = _schema_hash(tool)
    assert actual_hashes == expected_hashes


def _check_tool_inventory_is_registered_by_the_plugin() -> None:
    """All manifest schemas reach a fake Hermes context at plugin load."""
    module_path = ROOT / "hermes_lark" / "openclaw_tools.py"
    assert module_path.is_file(), "hermes_lark/openclaw_tools.py is missing"
    module_name = "_hermes_lark_parity_openclaw_tools"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None and spec.loader is not None
    tool_module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = tool_module
    try:
        spec.loader.exec_module(tool_module)
    finally:
        sys.modules.pop(module_name, None)

    class FakeContext:
        """Record Hermes tool registration calls."""

        def __init__(self) -> None:
            self.tools: list[dict[str, Any]] = []

        def register_tool(self, **kwargs: Any) -> None:
            """Capture one registered tool."""
            self.tools.append(kwargs)

    context = FakeContext()
    tool_module.register(context)
    expected_hashes = _fixture()["tools"]["structural_schema_sha256"]
    assert len(context.tools) == 39
    assert [item["name"] for item in context.tools] == list(expected_hashes)
    for item in context.tools:
        assert item["toolset"] == "feishu"
        assert callable(item["handler"])
        assert set(item["schema"]) == {"name", "description", "parameters"}
        assert item["schema"]["name"] == item["name"]
        assert _schema_hash(item["schema"]) == expected_hashes[item["name"]]

    package_module = _parse("hermes_lark/__init__.py")
    package_register = _function(package_module, "register")
    register_aliases: set[str] = set()
    module_aliases: set[str] = set()
    for node in package_module.body:
        if isinstance(node, ast.ImportFrom):
            if node.module is not None and node.module.endswith("openclaw_tools"):
                register_aliases.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name == "register"
                )
            module_aliases.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "openclaw_tools"
            )
        elif isinstance(node, ast.Import):
            module_aliases.update(
                alias.asname or alias.name.rsplit(".", 1)[-1]
                for alias in node.names
                if alias.name.endswith("openclaw_tools")
            )
    calls_openclaw_register = any(
        isinstance(node, ast.Call)
        and (
            (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in module_aliases
                and node.func.attr == "register"
            )
            or (
                isinstance(node.func, ast.Name)
                and node.func.id in register_aliases
            )
        )
        for node in ast.walk(package_register)
    )
    assert register_aliases or module_aliases
    assert calls_openclaw_register


def _check_all_upstream_skills_are_bundled_and_registered() -> None:
    """The nine localized skills retain identity, references, and registration."""
    fixture = _fixture()
    expected = fixture["skills"]
    expected_files = fixture["skill_files_upstream_sha256"]
    upstream_references = fixture["skill_upstream_references"]
    runtime_references = fixture["skill_runtime_references"]
    skills_root = ROOT / "skills"
    actual = sorted(
        path.parent.name for path in skills_root.glob("*/SKILL.md") if path.is_file()
    )
    assert actual == sorted(expected)

    actual_files = {
        path.relative_to(ROOT).as_posix()
        for path in skills_root.rglob("*")
        if path.is_file()
    }
    assert actual_files == set(expected_files)
    assert all(
        re.fullmatch(r"[0-9a-f]{64}", digest)
        for digest in expected_files.values()
    )
    assert set(upstream_references) == set(expected)
    assert set(runtime_references) == set(expected)

    inventory = json.loads(TOOL_INVENTORY_PATH.read_text(encoding="utf-8"))
    registered_tools = {tool["name"] for tool in inventory["tools"]}
    command_aliases = {
        "feishu_auth",
        "feishu_diagnose",
        "feishu_doctor",
    }

    for skill_name in expected:
        metadata = _frontmatter(skills_root / skill_name / "SKILL.md")
        assert metadata.get("name") == skill_name
        assert metadata.get("description")
        skill_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((skills_root / skill_name).rglob("*.md"))
        )
        references = sorted(
            set(re.findall(r"\bfeishu_[a-z_][a-z0-9_]*\b", skill_text))
        )
        assert references == runtime_references[skill_name]
        for reference in references:
            if reference.endswith("_"):
                assert any(
                    tool_name.startswith(reference)
                    for tool_name in registered_tools
                )
            else:
                assert reference in registered_tools | command_aliases

    package_module = _parse("hermes_lark/__init__.py")
    register = _function(package_module, "register")
    skill_assignment = next(
        node
        for node in package_module.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "_SKILL_NAMES"
            for target in node.targets
        )
    )
    assert ast.literal_eval(skill_assignment.value) == tuple(expected)

    registration_loops = [
        node
        for node in ast.walk(register)
        if isinstance(node, ast.For)
        and isinstance(node.target, ast.Name)
        and isinstance(node.iter, ast.Name)
        and node.iter.id == "_SKILL_NAMES"
    ]
    assert len(registration_loops) == 1
    loop = registration_loops[0]
    calls = [
        node
        for node in ast.walk(loop)
        if isinstance(node, ast.Call) and _call_name(node) == "register_skill"
    ]
    assert len(calls) == 1
    keywords = {
        keyword.arg: keyword.value
        for keyword in calls[0].keywords
        if keyword.arg is not None
    }
    assert isinstance(keywords.get("name"), ast.Name)
    assert keywords["name"].id == loop.target.id


def _check_generated_bridge_matches_the_reviewed_artifact() -> None:
    """The vendored bridge matches the reviewed digest and provenance."""
    fixture = _fixture()
    expected = fixture["generated_bridge"]
    bridge_path = ROOT / expected["path"]
    assert bridge_path.is_file()
    digest = _file_hash(bridge_path)
    assert digest == expected["sha256"]
    sidecar = bridge_path.with_suffix(f"{bridge_path.suffix}.sha256")
    assert sidecar.read_text(encoding="ascii").split()[0] == digest
    banner = bridge_path.read_bytes()[:512].decode("utf-8", errors="replace")
    assert fixture["upstream"]["commit"] in banner


def _check_adapter_defaults_match_openclaw_behavior() -> None:
    """Parity-sensitive defaults remain aligned with pinned OpenClaw."""
    expected = _fixture()["adapter"]
    adapter = _parse("hermes_lark/adapter.py")
    defaults = _getenv_defaults(adapter)
    constants = _module_constants(adapter)

    for env_name, expected_default in expected["env_defaults"].items():
        assert defaults.get(env_name) == {expected_default}, (
            f"{env_name} defaults to {defaults.get(env_name)!r}, "
            f"expected {expected_default!r}"
        )
    for constant_name, expected_value in expected["constants"].items():
        assert constants.get(constant_name) == expected_value, (
            f"{constant_name} is {constants.get(constant_name)!r}, "
            f"expected {expected_value!r}"
        )


def _check_public_setup_is_websocket_only() -> None:
    """Interactive setup exposes only the supported WebSocket transport."""
    setup = _function(_parse("hermes_lark/adapter.py"), "interactive_setup")
    strings = {
        node.value
        for node in ast.walk(setup)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert not any("webhook" in value.lower() for value in strings)

    connection_saves = [
        node
        for node in ast.walk(setup)
        if isinstance(node, ast.Call)
        and _call_name(node) == "save_env_value"
        and len(node.args) >= 2
        and _literal(node.args[0]) == "FEISHU_CONNECTION_MODE"
    ]
    assert len(connection_saves) == 1
    assert _literal(connection_saves[0].args[1]) == "websocket"


def _check_adapter_subscribes_to_the_upstream_event_surface() -> None:
    """Supported WebSocket callback registrations stay intact."""
    expected = _fixture()["adapter"]
    adapter = _parse("hermes_lark/adapter.py")
    handler = _function(adapter, "_build_event_handler")

    callback_names = {
        node.func.attr
        for node in ast.walk(handler)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert set(expected["sdk_callbacks"]) <= callback_names

    string_literals = {
        node.value
        for node in ast.walk(handler)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert set(expected["custom_events"]) <= string_literals


class ParityContractTest(unittest.TestCase):
    """Run the pinned static contract with only the Python standard library."""

    def test_package_entrypoint_replaces_bundled_platform_key(self) -> None:
        """The pip plugin wins Hermes' path-derived Feishu key."""
        pyproject = tomllib.loads(_read("pyproject.toml"))

        self.assertEqual(
            pyproject["project"]["entry-points"]["hermes_agent.plugins"],
            {"platforms/feishu": "hermes_lark"},
        )

    def test_plugin_manifest_covers_adapter_configuration(self) -> None:
        """The plugin manifest covers parity-critical configuration."""
        _check_plugin_manifest_covers_adapter_configuration()

    def test_adapter_registers_the_complete_platform_surface(self) -> None:
        """The adapter registers the complete Hermes platform surface."""
        _check_adapter_registers_the_complete_platform_surface()

    def test_all_upstream_tool_schemas_are_preserved(self) -> None:
        """All 39 upstream tool schemas are structurally unchanged."""
        _check_all_upstream_tool_schemas_are_preserved()

    def test_localization_projection_keeps_description_named_inputs(self) -> None:
        """A field named description remains part of the structural contract."""
        projected = _without_descriptions(
            {
                "description": "Localized schema annotation",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "Localized input annotation",
                    }
                },
            }
        )

        self.assertEqual(
            projected,
            {"properties": {"description": {"type": "string"}}},
        )

    def test_tool_inventory_is_registered_by_the_plugin(self) -> None:
        """The static inventory is connected to Hermes tool registration."""
        _check_tool_inventory_is_registered_by_the_plugin()

    def test_all_upstream_skills_are_bundled_and_registered(self) -> None:
        """All nine upstream skills are bundled and registered."""
        _check_all_upstream_skills_are_bundled_and_registered()

    def test_generated_bridge_matches_the_reviewed_artifact(self) -> None:
        """The generated bridge matches its reviewed digest."""
        _check_generated_bridge_matches_the_reviewed_artifact()

    def test_adapter_defaults_match_openclaw_behavior(self) -> None:
        """Parity-sensitive adapter defaults match OpenClaw."""
        _check_adapter_defaults_match_openclaw_behavior()

    def test_public_setup_is_websocket_only(self) -> None:
        """Interactive setup only exposes the supported transport."""
        _check_public_setup_is_websocket_only()

    def test_adapter_subscribes_to_the_upstream_event_surface(self) -> None:
        """The adapter subscribes to the upstream event surface."""
        _check_adapter_subscribes_to_the_upstream_event_surface()


if __name__ == "__main__":
    unittest.main()
