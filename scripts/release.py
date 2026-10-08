#!/usr/bin/env python3
"""Generate listing metadata, validate self-contained plugins, and build stable ZIPs."""
import argparse
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
TEXT_SUFFIXES = {".py", ".md", ".json", ".csv", ".txt", ".svg", ".java", ".swift", ".pyj", ".yaml", ".yml", ".toml", ".html", ".js", ".css"}
FORBIDDEN_NAMES = {".env", ".DS_Store", ".app.json", ".mcp.json", "mcp.json", "credentials.json", "id_rsa", "id_ed25519"}
EXCLUDED_DIRS = {"__pycache__", ".git", ".pytest_cache", ".venv", "node_modules"}
SECRET_PATTERN = re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{30,}|sk-(?:proj-)?[A-Za-z0-9_-]{30,}|-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----)")

ICON_SHAPES = {
    "frame-lab": '<path d="M12 40h8l5-17 9 27 7-18h11"/><path d="M12 14h12M12 14v10M52 14H40M52 14v10M12 52h12M12 52V42M52 52H40M52 52V42"/>',
    "fit-to-upload": '<rect x="12" y="18" width="40" height="31" rx="5"/><path d="m27 25 13 8-13 8z"/><path d="M20 10v8M44 10v8M20 54h24"/>',
    "tablebeam": '<rect x="12" y="12" width="40" height="40" rx="4"/><path d="M12 25h40M26 12v40M12 38h40"/><path d="m34 43 4 4 8-9"/>',
    "shipproof": '<path d="m32 9 20 9v15c0 11-9 18-20 22-11-4-20-11-20-22V18z"/><path d="m22 32 7 7 14-15"/>',
    "safe-tidy": '<path d="M10 21V15h17l6 7h21v28H10z"/><path d="M23 33h17M23 40h10"/><path d="m38 36 5-5 5 5M43 31v12"/>',
    "cad-rescue": '<path d="M12 50V14h32v24H28v12zM12 27h16M28 14v24"/><path d="M38 48h15M45 41v14"/>',
    "ci-spend-check": '<path d="M13 49V35M26 49V25M39 49V14M10 54h44"/><circle cx="48" cy="37" r="9"/><path d="M48 32v6l4 2"/>',
    "asset-check": '<path d="m32 9 21 12v24L32 57 11 45V21zM11 21l21 12 21-12M32 33v24M22 15l21 12"/>'
}


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def dump_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def generate_metadata(root=ROOT):
    catalog = read_json(root / "catalog.json")
    market = {"name": catalog["name"], "interface": {"displayName": catalog["display_name"]}, "plugins": []}
    for item in catalog["plugins"]:
        plugin_id = item["id"]
        plugin = root / "plugins" / plugin_id
        (plugin / "assets").mkdir(parents=True, exist_ok=True)
        icon = f'<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64"><rect width="64" height="64" rx="15" fill="{item["color"]}"/><g fill="none" stroke="#fffaf2" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round">{ICON_SHAPES[plugin_id]}</g></svg>\n'
        (plugin / "assets" / "icon.svg").write_text(icon, encoding="utf-8")
        license_id = item.get("license", "MIT")
        if license_id == "MIT" and not (plugin / "LICENSE").exists():
            (plugin / "LICENSE").write_bytes((root / "LICENSE").read_bytes())
        elif license_id != "MIT" and not (plugin / "LICENSE").exists():
            raise ValueError(f"{plugin_id}: provide the {license_id} license before generating metadata")
        homepage = catalog["website"] + "/#" + plugin_id
        interface = {
            "displayName": item["name"], "shortDescription": item["short"],
            "longDescription": item["description"], "developerName": catalog["publisher"],
            "category": item["category"], "capabilities": item["capabilities"],
            "websiteURL": homepage, "supportURL": catalog["website"] + "/support.html",
            "privacyPolicyURL": catalog["website"] + "/privacy.html",
            "termsOfServiceURL": catalog["website"] + "/terms.html",
            "defaultPrompt": item["prompts"], "brandColor": item["color"],
            "composerIcon": "./assets/icon.svg", "logo": "./assets/icon.svg",
        }
        manifest = {
            "$schema": SCHEMA, "name": plugin_id, "version": catalog["version"],
            "description": item["description"], "author": {"name": catalog["publisher"], "url": catalog["repository"]},
            "homepage": homepage, "repository": catalog["repository"], "license": license_id,
            "keywords": item["keywords"],
            "extensions": {"com.openai": {"interface": interface, "onboardingSkill": f"./skills/{plugin_id}/SKILL.md"}},
        }
        dump_json(plugin / "plugin.json", manifest)
        # Codex 0.157 local marketplaces still accept the compatibility manifest.
        # Identity remains canonical in plugin.json on portable-aware hosts.
        compat = {k: v for k, v in manifest.items() if k not in {"$schema", "extensions"}}
        compat.update({"skills": "./skills/", "interface": interface})
        dump_json(plugin / ".codex-plugin" / "plugin.json", compat)
        market["plugins"].append({"name": plugin_id, "source": {"source": "local", "path": "./plugins/" + plugin_id}, "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"}, "category": item["category"]})
    dump_json(root / ".agents" / "plugins" / "marketplace.json", market)
    return len(catalog["plugins"])


def validate_reference(plugin, value, field, errors):
    if not isinstance(value, str) or not value.startswith("./"):
        errors.append(f"{field}: expected a ./-prefixed file path")
        return
    path = plugin / value[2:]
    try:
        path.resolve().relative_to(plugin.resolve())
    except ValueError:
        errors.append(f"{field}: path escapes the plugin")
        return
    if not path.is_file() or path.is_symlink():
        errors.append(f"{field}: referenced file is absent or a symlink: {value}")
        return
    if path.stat().st_size > 5 * 1024 * 1024:
        errors.append(f"{field}: asset exceeds 5 MiB")
    if path.suffix == ".svg":
        try:
            xml = ElementTree.parse(path).getroot()
            box = [float(x) for x in xml.attrib.get("viewBox", "").split()]
            width = float(xml.attrib.get("width", box[2] if len(box) == 4 else 0))
            height = float(xml.attrib.get("height", box[3] if len(box) == 4 else 0))
            if width != height or width < 48:
                errors.append(f"{field}: icon must be square and at least 48 pixels")
            if any(n.tag.rsplit("}", 1)[-1] in {"script", "foreignObject"} for n in xml.iter()):
                errors.append(f"{field}: active SVG content is forbidden")
        except (ValueError, ElementTree.ParseError):
            errors.append(f"{field}: invalid SVG")


def plugin_files(plugin):
    for path in sorted(plugin.rglob("*")):
        rel = path.relative_to(plugin)
        if path.is_symlink():
            raise ValueError(f"symlink cannot be distributed: {rel}")
        if any(part in EXCLUDED_DIRS for part in rel.parts):
            continue
        if path.is_file():
            yield path


def validate_plugin(plugin):
    errors = []
    try:
        manifest = read_json(plugin / "plugin.json")
    except (OSError, ValueError) as exc:
        return [f"plugin.json: {exc}"]
    if manifest.get("$schema") != SCHEMA:
        errors.append("plugin.json: unsupported or missing portable schema")
    name = manifest.get("name", "")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or len(name) > 64:
        errors.append("name: invalid plugin identifier")
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[a-zA-Z0-9.-]+)?", str(manifest.get("version", ""))):
        errors.append("version: semantic version required")
    interface = manifest.get("extensions", {}).get("com.openai", {}).get("interface", {})
    for key, limit in {"displayName": 30, "shortDescription": 30, "longDescription": 4000, "developerName": 80, "category": 120}.items():
        value = interface.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            errors.append(f"{key}: required nonempty text of at most {limit} characters")
    prompts = interface.get("defaultPrompt", [])
    if not isinstance(prompts, list) or not 1 <= len(prompts) <= 3 or any(not isinstance(x, str) or not x.strip() or len(x) > 128 for x in prompts):
        errors.append("defaultPrompt: supply one to three prompts, each at most 128 characters")
    elif len(set(prompts)) != len(prompts):
        errors.append("defaultPrompt: duplicate prompts")
    for field in ("websiteURL", "supportURL", "privacyPolicyURL", "termsOfServiceURL"):
        if field in interface:
            parts = urlsplit(interface[field])
            if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
                errors.append(f"{field}: public HTTPS URL required")
    for field in ("logo", "composerIcon"):
        validate_reference(plugin, interface.get(field), field, errors)
    if interface.get("screenshots"):
        errors.append("screenshots: omit for skills-only submissions")
    skill_files = list((plugin / "skills").glob("*/SKILL.md"))
    if not skill_files:
        errors.append("skills: at least one SKILL.md is required")
    for skill in skill_files:
        text = skill.read_text(encoding="utf-8")
        parts = text.split("---", 2)
        if len(parts) != 3 or parts[0].strip() or not re.search(r"(?m)^name:\s*\S", parts[1]) or not re.search(r"(?m)^description:\s*\S", parts[1]):
            errors.append(f"{skill.relative_to(plugin)}: valid name/description frontmatter required")
    if not (plugin / "LICENSE").is_file():
        errors.append("LICENSE: license file is required")
    try:
        files = list(plugin_files(plugin))
    except ValueError as exc:
        errors.append(str(exc))
        return errors
    for path in files:
        rel = path.relative_to(plugin)
        if path.name in FORBIDDEN_NAMES or path.suffix in {".pem", ".key", ".pyc", ".class", ".jar"} or path.name.startswith(".env."):
            errors.append(f"forbidden release file: {rel}")
        if path.suffix in TEXT_SUFFIXES:
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeError:
                errors.append(f"invalid UTF-8 text: {rel}")
                continue
            if re.search(r"(?:/Users/[^/\s]+/|/home/[^/\s]+/|[A-Za-z]:\\Users\\[^\\\s]+\\)", content):
                errors.append(f"private machine path in {rel}")
            if SECRET_PATTERN.search(content):
                errors.append(f"possible credential in {rel}")
    return errors


def package_plugin(plugin, output):
    errors = validate_plugin(plugin)
    if errors:
        raise ValueError("; ".join(errors))
    manifest = read_json(plugin / "plugin.json")
    output.mkdir(parents=True, exist_ok=True)
    target = output / f'{manifest["name"]}-{manifest["version"]}.zip'
    temporary = target.with_suffix(".zip.tmp")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for path in plugin_files(plugin):
                rel = path.relative_to(plugin).as_posix()
                info = zipfile.ZipInfo(rel, date_time=(2026, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                archive.writestr(info, path.read_bytes())
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return {"name": manifest["name"], "version": manifest["version"], "path": str(target), "file": target.name, "bytes": target.stat().st_size, "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["metadata", "validate", "build"])
    parser.add_argument("--plugin", help="One catalog plugin ID; default is all")
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    catalog = read_json(ROOT / "catalog.json")
    items = [p for p in catalog["plugins"] if not args.plugin or p["id"] == args.plugin]
    if not items:
        parser.error("plugin is not in the catalog")
    if args.command == "metadata":
        print(json.dumps({"generated": generate_metadata()}))
        return 0
    checks = {p["id"]: validate_plugin(ROOT / "plugins" / p["id"]) for p in items}
    if any(checks.values()):
        print(json.dumps({"valid": False, "errors": {k: v for k, v in checks.items() if v}}, indent=2))
        return 1
    if args.command == "validate":
        print(json.dumps({"valid": True, "plugins": list(checks)}))
        return 0
    releases = [package_plugin(ROOT / "plugins" / p["id"], args.output) for p in items]
    public = [{k: v for k, v in release.items() if k != "path"} for release in releases]
    dump_json(args.output / "releases.json", {"version": catalog["version"], "plugins": public})
    (args.output / "SHA256SUMS").write_text("".join(f'{r["sha256"]}  {r["file"]}\n' for r in releases), encoding="utf-8")
    print(json.dumps({"built": public}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
