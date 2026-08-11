import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { createHash } from "node:crypto";
import { execFile, spawn } from "node:child_process";
import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);
const MAX_FILE_BYTES = 1024 * 1024;
const MAX_WORK_PROFILE_BYTES = 256 * 1024;
const MAX_OUTPUT_BYTES = 1024 * 1024;
const MAX_SEARCH_RESULTS = 200;
const MAX_ARG_COUNT = 96;
const MAX_ARG_BYTES = 64 * 1024;
const COMMAND_TIMEOUT_MS = 120_000;
const RECEIPT_PROTOCOL_VERSION = 1;
const MAX_MEMORY_REQUEST_BYTES = 16 * 1024 * 1024;
const MEMORY_BRIDGE_TIMEOUT_MS = 150_000;
const MAX_EXA_REQUEST_BYTES = 64 * 1024;
const EXA_BRIDGE_TIMEOUT_MS = 120_000;
const MAX_XIAOQING_REQUEST_BYTES = 1024 * 1024;
const XIAOQING_BRIDGE_TIMEOUT_MS = 150_000;
const MAX_DINGTALK_IMAGE_REQUEST_BYTES = 16 * 1024;
const MAX_DINGTALK_IMAGE_RESPONSE_BYTES = 15 * 1024 * 1024;
const DINGTALK_IMAGE_BRIDGE_TIMEOUT_MS = 150_000;
const MAX_GRAPHIFY_VALUE_BYTES = 16 * 1024;
const MAX_REVIEWED_IMAGE_BYTES = 10 * 1024 * 1024;
const BLOCKED_COMMAND_SEGMENTS = new Set([
	"auth",
	"authorize",
	"install",
	"login",
	"logout",
	"reset",
	"uninstall",
	"upgrade",
]);
const PROVIDER_SECRET_ENV_PATTERNS = [
	/API_KEY/i,
	/AUTHORIZATION/i,
	/BEARER/i,
	/CLIENT_SECRET/i,
	/PRIVATE_KEY/i,
	/^CEO_PI_/i,
];

type DwsEffect = "read" | "write" | "destructive";

interface DwsToolMetadata {
	cli_path: string;
	effect: DwsEffect;
	availability?: string;
	confirmation?: string;
}

interface LarkToolMetadata {
	name: string;
	description?: string;
	_meta?: {
		risk?: "read" | "write" | "high-risk-write";
		danger?: boolean;
	};
}

let dwsMetadataPromise: Promise<DwsToolMetadata[]> | undefined;
let larkMetadataPromise: Promise<LarkToolMetadata[]> | undefined;
const larkShortcutMetadataPromises = new Map<string, Promise<LarkToolMetadata>>();

function safeChildEnvironment(): NodeJS.ProcessEnv {
	const env = { ...process.env };
	for (const key of Object.keys(env)) {
		if (PROVIDER_SECRET_ENV_PATTERNS.some((pattern) => pattern.test(key))) {
			delete env[key];
		}
	}
	return env;
}

function configuredReadRoots(cwd: string): string[] {
	const configured = process.env.CEO_PI_ALLOWED_READ_ROOTS ?? "";
	const candidates = configured
		.split(path.delimiter)
		.map((item) => item.trim())
		.filter(Boolean);
	if (candidates.length === 0) candidates.push(cwd);
	return [...new Set(candidates.map((item) => path.resolve(item)))];
}

async function existingRealPath(candidate: string): Promise<string> {
	try {
		return await fs.realpath(candidate);
	} catch {
		return path.resolve(candidate);
	}
}

async function resolveAllowedPath(
	rawPath: string,
	cwd: string,
	options: { mustExist?: boolean; fileOnly?: boolean } = {},
): Promise<string> {
	if (!rawPath.trim() || rawPath.includes("\0")) throw new Error("path_invalid");
	const resolved = path.resolve(cwd, rawPath);
	const candidate = await existingRealPath(resolved);
	const roots = await Promise.all(configuredReadRoots(cwd).map(existingRealPath));
	const allowed = roots.some((root) => candidate === root || candidate.startsWith(`${root}${path.sep}`));
	if (!allowed) throw new Error("path_outside_reviewed_roots");
	if (options.mustExist !== false) {
		const stat = await fs.stat(candidate);
		if (options.fileOnly && !stat.isFile()) throw new Error("path_not_file");
	}
	return candidate;
}

function boundedText(value: string, maximum = MAX_OUTPUT_BYTES): string {
	const encoded = Buffer.from(value, "utf8");
	if (encoded.length <= maximum) return value;
	return `${encoded.subarray(0, maximum).toString("utf8")}\n[output truncated]`;
}

function validateArgv(argv: string[]): void {
	if (argv.length < 2 || argv.length > MAX_ARG_COUNT) throw new Error("reviewed_argv_invalid");
	if (path.basename(argv[0]) !== "dws") throw new Error("reviewed_cli_not_allowed");
	let byteCount = 0;
	for (const argument of argv) {
		if (argument.includes("\0") || argument.includes("\n") || argument.includes("\r")) {
			throw new Error("reviewed_argv_control_character");
		}
		byteCount += Buffer.byteLength(argument, "utf8");
	}
	if (byteCount > MAX_ARG_BYTES) throw new Error("reviewed_argv_too_large");
}

function configuredLarkBinary(): string {
	const configured = process.env.CEO_FEISHU_CLI_BINARY?.trim() || "lark-cli";
	if (path.basename(configured) !== "lark-cli") throw new Error("reviewed_lark_binary_invalid");
	return configured;
}

function configuredGraphifyBinary(): string {
	const configured = process.env.CEO_GRAPHIFY_BINARY?.trim() || "graphify";
	if (path.basename(configured) !== "graphify") throw new Error("reviewed_graphify_binary_invalid");
	return configured;
}

function validateGraphifyValue(value: string): string {
	const normalized = value.trim();
	if (!normalized) throw new Error("reviewed_graphify_argument_missing");
	if (normalized.includes("\0") || normalized.includes("\n") || normalized.includes("\r")) {
		throw new Error("reviewed_graphify_control_character");
	}
	if (Buffer.byteLength(normalized, "utf8") > MAX_GRAPHIFY_VALUE_BYTES) {
		throw new Error("reviewed_graphify_argument_too_large");
	}
	return normalized;
}

function validateLarkArgv(argv: string[]): void {
	if (argv.length < 2 || argv.length > MAX_ARG_COUNT) throw new Error("reviewed_argv_invalid");
	if (path.basename(argv[0]) !== "lark-cli") throw new Error("reviewed_cli_not_allowed");
	let byteCount = 0;
	for (const argument of argv) {
		if (argument.includes("\0") || argument.includes("\n") || argument.includes("\r")) {
			throw new Error("reviewed_argv_control_character");
		}
		byteCount += Buffer.byteLength(argument, "utf8");
	}
	if (byteCount > MAX_ARG_BYTES) throw new Error("reviewed_argv_too_large");
}

async function loadDwsMetadata(): Promise<DwsToolMetadata[]> {
	if (!dwsMetadataPromise) {
		dwsMetadataPromise = (async () => {
			const result = await execFileAsync("dws", ["schema", "--all", "--compact", "--format", "json"], {
				env: safeChildEnvironment(),
				timeout: 30_000,
				maxBuffer: 8 * 1024 * 1024,
			});
			const payload = JSON.parse(result.stdout) as { products?: Array<{ tools?: DwsToolMetadata[] }> };
			const tools = payload.products?.flatMap((product) => product.tools ?? []) ?? [];
			return tools.filter(
				(tool) =>
					typeof tool.cli_path === "string" &&
					(tool.effect === "read" || tool.effect === "write" || tool.effect === "destructive"),
			);
		})().catch((error) => {
			dwsMetadataPromise = undefined;
			throw error;
		});
	}
	return dwsMetadataPromise;
}

async function loadLarkMetadata(): Promise<LarkToolMetadata[]> {
	if (!larkMetadataPromise) {
		larkMetadataPromise = (async () => {
			const result = await execFileAsync(configuredLarkBinary(), ["schema"], {
				env: safeChildEnvironment(),
				timeout: 60_000,
				maxBuffer: 8 * 1024 * 1024,
			});
			const payload = JSON.parse(result.stdout) as unknown;
			if (!Array.isArray(payload)) throw new Error("reviewed_lark_schema_invalid");
			return payload.filter((tool): tool is LarkToolMetadata => {
				if (!tool || typeof tool !== "object") return false;
				const candidate = tool as LarkToolMetadata;
				return (
					typeof candidate.name === "string" &&
					(candidate._meta?.risk === "read" ||
						candidate._meta?.risk === "write" ||
						candidate._meta?.risk === "high-risk-write")
				);
			});
		})().catch((error) => {
			larkMetadataPromise = undefined;
			throw error;
		});
	}
	return larkMetadataPromise;
}

async function loadLarkShortcutMetadata(argv: string[]): Promise<LarkToolMetadata | undefined> {
	const commandTokens = argv.slice(1).filter((token) => !token.startsWith("-"));
	if (
		commandTokens.length < 2 ||
		!/^[a-z][a-z0-9_-]*$/.test(commandTokens[0]) ||
		!/^\+[a-z][a-z0-9_-]*$/.test(commandTokens[1])
	) return undefined;
	const command = `${commandTokens[0]} ${commandTokens[1]}`;
	let pending = larkShortcutMetadataPromises.get(command);
	if (!pending) {
		pending = (async () => {
			const result = await execFileAsync(configuredLarkBinary(), [commandTokens[0], commandTokens[1], "--help"], {
				env: safeChildEnvironment(),
				timeout: 30_000,
				maxBuffer: MAX_OUTPUT_BYTES,
			});
			const match = result.stdout.match(/^Risk:\s*(read|write|high-risk-write)\s*$/m);
			if (!match) throw new Error("reviewed_lark_shortcut_risk_missing");
			return {
				name: command,
				_meta: {
					risk: match[1] as "read" | "write" | "high-risk-write",
					danger: match[1] === "high-risk-write",
				},
			};
		})().catch((error) => {
			larkShortcutMetadataPromises.delete(command);
			throw error;
		});
		larkShortcutMetadataPromises.set(command, pending);
	}
	return pending;
}

function commandMetadata(argv: string[], tools: DwsToolMetadata[]): DwsToolMetadata | undefined {
	const commandTokens = argv.slice(1);
	return tools
		.filter((tool) => {
			const tokens = tool.cli_path.split(/\s+/).filter(Boolean);
			return tokens.every((token, index) => commandTokens[index] === token);
		})
		.sort((left, right) => right.cli_path.split(/\s+/).length - left.cli_path.split(/\s+/).length)[0];
}

function larkCommandMetadata(argv: string[], tools: LarkToolMetadata[]): LarkToolMetadata | undefined {
	const commandTokens = argv.slice(1);
	return tools
		.filter((tool) => {
			const tokens = tool.name.split(/\s+/).filter(Boolean);
			return tokens.every((token, index) => commandTokens[index] === token);
		})
		.sort((left, right) => right.name.split(/\s+/).length - left.name.split(/\s+/).length)[0];
}

function commandDigest(argv: string[]): string {
	return createHash("sha256").update(JSON.stringify(argv)).digest("hex");
}

function reviewedImageMimeType(data: Buffer): string | undefined {
	if (data.length >= 8 && data.subarray(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]))) {
		return "image/png";
	}
	if (data.length >= 3 && data[0] === 0xff && data[1] === 0xd8 && data[2] === 0xff) return "image/jpeg";
	if (data.length >= 6) {
		const header = data.subarray(0, 6).toString("ascii");
		if (header === "GIF87a" || header === "GIF89a") return "image/gif";
	}
	if (
		data.length >= 12 &&
		data.subarray(0, 4).toString("ascii") === "RIFF" &&
		data.subarray(8, 12).toString("ascii") === "WEBP"
	) return "image/webp";
	return undefined;
}

async function readReviewedImage(imagePath: string) {
	const stat = await fs.stat(imagePath);
	if (!stat.isFile() || stat.size <= 0) throw new Error("reviewed_image_missing");
	if (stat.size > MAX_REVIEWED_IMAGE_BYTES) throw new Error("reviewed_image_too_large");
	const data = await fs.readFile(imagePath);
	const mimeType = reviewedImageMimeType(data);
	if (!mimeType) throw new Error("reviewed_image_format_unsupported");
	return {
		content: { type: "image" as const, data: data.toString("base64"), mimeType },
		digest: createHash("sha256").update(data).digest("hex"),
		byteLength: data.length,
	};
}

function replaceOutputPath(argv: string[], outputPath: string): string[] {
	const rewritten = [...argv];
	const index = rewritten.indexOf("--output");
	if (index >= 0) {
		if (index + 1 >= rewritten.length) throw new Error("reviewed_image_output_invalid");
		rewritten[index + 1] = outputPath;
		return rewritten;
	}
	rewritten.push("--output", outputPath);
	return rewritten;
}

function targetIdentifiers(argv: string[]): Record<string, string> {
	const identifiers: Record<string, string> = {};
	for (let index = 1; index < argv.length; index += 1) {
		const token = argv[index];
		if (!token.startsWith("--")) continue;
		const [rawFlag, inlineValue] = token.slice(2).split(/=(.*)/s, 2);
		const normalized = rawFlag.replaceAll("_", "-").toLowerCase();
		const isTarget =
			normalized.endsWith("-id") ||
			new Set(["id", "conversation", "group", "email", "node", "url"]).has(normalized);
		if (!isTarget) continue;
		let value = inlineValue ?? "";
		if (!inlineValue && index + 1 < argv.length && !argv[index + 1].startsWith("-")) {
			value = argv[index + 1];
			index += 1;
		}
		if (!value || /(?:api[_-]?key|bearer|password|secret|token)/i.test(value)) continue;
		identifiers[normalized] = value.slice(0, 500);
		if (Object.keys(identifiers).length >= 32) break;
	}
	return identifiers;
}

function stableJson(value: unknown): string {
	if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
	if (value && typeof value === "object") {
		const record = value as Record<string, unknown>;
		return `{${Object.keys(record)
			.sort()
			.map((key) => `${JSON.stringify(key)}:${stableJson(record[key])}`)
			.join(",")}}`;
	}
	return JSON.stringify(value);
}

function memoryOperationDigest(tool: string, args: Record<string, unknown>): string {
	return createHash("sha256").update(stableJson({ tool, arguments: args })).digest("hex");
}

function structuredTargetIdentifiers(value: unknown): Record<string, string> {
	const identifiers: Record<string, string> = {};
	const stack: unknown[] = [value];
	while (stack.length > 0 && Object.keys(identifiers).length < 32) {
		const current = stack.pop();
		if (Array.isArray(current)) {
			stack.push(...current.slice(0, 64));
			continue;
		}
		if (!current || typeof current !== "object") continue;
		for (const [key, item] of Object.entries(current as Record<string, unknown>)) {
			const normalized = key.replaceAll("_", "").replaceAll("-", "").toLowerCase();
			const target = normalized === "id" || normalized.endsWith("id") || normalized.endsWith("url") || normalized === "uuid";
			if (target && typeof item === "string" && item && !/(?:api[_-]?key|bearer|password|secret|token)/i.test(item)) {
				identifiers[key] = item.slice(0, 500);
			} else if (item && typeof item === "object") {
				stack.push(item);
			}
		}
	}
	return identifiers;
}

function safeMemoryEnvironment(): NodeJS.ProcessEnv {
	const env = safeChildEnvironment();
	for (const key of [
		"MEMORY_CONNECTOR_URL",
		"CONNECTOR_API_KEY",
		"MEMORY_CONNECTOR_AUTH_TYPE",
		"MEMORY_CONNECTOR_CONTENT_TYPE",
	]) {
		const value = process.env[key];
		if (value) env[key] = value;
	}
	delete env.MEMORY_CONNECTOR_USER_ID;
	return env;
}

function safeExaEnvironment(): NodeJS.ProcessEnv {
	const env = safeChildEnvironment();
	const url = process.env.CEO_PI_EXA_MCP_URL;
	if (url) env.CEO_PI_EXA_MCP_URL = url;
	return env;
}

function safeXiaoqingEnvironment(): NodeJS.ProcessEnv {
	const env = safeChildEnvironment();
	for (const key of ["CEO_PI_XIAOQING_MCP_URL", "CEO_PI_XIAOQING_ACCESS_TOKEN"]) {
		const value = process.env[key];
		if (value) env[key] = value;
	}
	return env;
}

function safeDingtalkImageEnvironment(): NodeJS.ProcessEnv {
	const env = safeChildEnvironment();
	const bridge = process.env.CEO_PI_DINGTALK_IMAGE_BRIDGE_PATH;
	if (bridge) env.CEO_PI_DINGTALK_IMAGE_BRIDGE_PATH = bridge;
	return env;
}

interface MemoryBridgeResponse {
	ok: true;
	tool: string;
	result: unknown;
	effect?: "read" | "write";
	confirmed: boolean;
	receipt: Record<string, string>;
}

async function callExaBridge(
	tool: string,
	args: Record<string, unknown>,
	signal: AbortSignal,
): Promise<MemoryBridgeResponse> {
	const python = process.env.CEO_PI_PYTHON_BINARY?.trim();
	const bridge = process.env.CEO_PI_EXA_BRIDGE_PATH?.trim();
	if (!python || !bridge || !path.isAbsolute(bridge)) throw new Error("exa_bridge_not_configured");
	const request = JSON.stringify({ tool, arguments: args });
	if (Buffer.byteLength(request, "utf8") > MAX_EXA_REQUEST_BYTES) throw new Error("exa_request_too_large");
	return await new Promise<MemoryBridgeResponse>((resolve, reject) => {
		const child = spawn(python, [bridge], {
			env: safeExaEnvironment(),
			stdio: ["pipe", "pipe", "pipe"],
		});
		const stdout: Buffer[] = [];
		const stderr: Buffer[] = [];
		let stdoutBytes = 0;
		let stderrBytes = 0;
		let settled = false;
		const finish = (callback: () => void) => {
			if (settled) return;
			settled = true;
			clearTimeout(timer);
			signal.removeEventListener("abort", abort);
			callback();
		};
		const abort = () => {
			child.kill("SIGTERM");
			finish(() => reject(new Error("exa_bridge_aborted")));
		};
		const timer = setTimeout(() => {
			child.kill("SIGTERM");
			finish(() => reject(new Error("exa_bridge_timeout")));
		}, EXA_BRIDGE_TIMEOUT_MS);
		signal.addEventListener("abort", abort, { once: true });
		child.on("error", (error) => finish(() => reject(error)));
		child.stdout.on("data", (chunk: Buffer) => {
			stdoutBytes += chunk.length;
			if (stdoutBytes > MAX_OUTPUT_BYTES) {
				child.kill("SIGTERM");
				finish(() => reject(new Error("exa_bridge_output_too_large")));
				return;
			}
			stdout.push(chunk);
		});
		child.stderr.on("data", (chunk: Buffer) => {
			stderrBytes += chunk.length;
			if (stderrBytes <= MAX_OUTPUT_BYTES) stderr.push(chunk);
		});
		child.on("close", (code) => {
			finish(() => {
				if (code !== 0) {
					reject(new Error(boundedText(Buffer.concat(stderr).toString("utf8")) || "exa_bridge_failed"));
					return;
				}
				try {
					const response = JSON.parse(Buffer.concat(stdout).toString("utf8")) as MemoryBridgeResponse;
					if (response.ok !== true || response.tool !== tool) throw new Error("exa_bridge_response_invalid");
					resolve(response);
				} catch (error) {
					reject(error instanceof Error ? error : new Error("exa_bridge_response_invalid"));
				}
			});
		});
		child.stdin.end(request);
	});
}

async function callMemoryBridge(
	tool: string,
	args: Record<string, unknown>,
	signal: AbortSignal,
): Promise<MemoryBridgeResponse> {
	const python = process.env.CEO_PI_PYTHON_BINARY?.trim();
	const bridge = process.env.CEO_PI_MEMORY_BRIDGE_PATH?.trim();
	if (!python || !bridge || !path.isAbsolute(bridge)) throw new Error("memory_bridge_not_configured");
	const request = JSON.stringify({ tool, arguments: args });
	if (Buffer.byteLength(request, "utf8") > MAX_MEMORY_REQUEST_BYTES) throw new Error("memory_request_too_large");
	return await new Promise<MemoryBridgeResponse>((resolve, reject) => {
		const child = spawn(python, [bridge], {
			env: safeMemoryEnvironment(),
			stdio: ["pipe", "pipe", "pipe"],
		});
		const stdout: Buffer[] = [];
		const stderr: Buffer[] = [];
		let stdoutBytes = 0;
		let stderrBytes = 0;
		let settled = false;
		const finish = (callback: () => void) => {
			if (settled) return;
			settled = true;
			clearTimeout(timer);
			signal.removeEventListener("abort", abort);
			callback();
		};
		const abort = () => {
			child.kill("SIGTERM");
			finish(() => reject(new Error("memory_bridge_aborted")));
		};
		const timer = setTimeout(() => {
			child.kill("SIGTERM");
			finish(() => reject(new Error("memory_bridge_timeout")));
		}, MEMORY_BRIDGE_TIMEOUT_MS);
		signal.addEventListener("abort", abort, { once: true });
		child.on("error", (error) => finish(() => reject(error)));
		child.stdout.on("data", (chunk: Buffer) => {
			stdoutBytes += chunk.length;
			if (stdoutBytes > MAX_OUTPUT_BYTES) {
				child.kill("SIGTERM");
				finish(() => reject(new Error("memory_bridge_output_too_large")));
				return;
			}
			stdout.push(chunk);
		});
		child.stderr.on("data", (chunk: Buffer) => {
			stderrBytes += chunk.length;
			if (stderrBytes <= MAX_OUTPUT_BYTES) stderr.push(chunk);
		});
		child.on("close", (code) => {
			finish(() => {
				if (code !== 0) {
					reject(new Error(boundedText(Buffer.concat(stderr).toString("utf8")) || "memory_bridge_failed"));
					return;
				}
				try {
					const response = JSON.parse(Buffer.concat(stdout).toString("utf8")) as MemoryBridgeResponse;
					if (response.ok !== true || response.tool !== tool) throw new Error("memory_bridge_response_invalid");
					resolve(response);
				} catch (error) {
					reject(error instanceof Error ? error : new Error("memory_bridge_response_invalid"));
				}
			});
		});
		child.stdin.end(request);
	});
}

async function callXiaoqingBridge(
	tool: string,
	args: Record<string, unknown>,
	signal: AbortSignal,
): Promise<MemoryBridgeResponse> {
	const python = process.env.CEO_PI_PYTHON_BINARY?.trim();
	const bridge = process.env.CEO_PI_XIAOQING_BRIDGE_PATH?.trim();
	if (!python || !bridge || !path.isAbsolute(bridge)) throw new Error("xiaoqing_bridge_not_configured");
	const request = JSON.stringify({ tool, arguments: args });
	if (Buffer.byteLength(request, "utf8") > MAX_XIAOQING_REQUEST_BYTES) throw new Error("xiaoqing_request_too_large");
	return await new Promise<MemoryBridgeResponse>((resolve, reject) => {
		const child = spawn(python, [bridge], {
			env: safeXiaoqingEnvironment(),
			stdio: ["pipe", "pipe", "pipe"],
		});
		const stdout: Buffer[] = [];
		const stderr: Buffer[] = [];
		let stdoutBytes = 0;
		let stderrBytes = 0;
		let settled = false;
		const finish = (callback: () => void) => {
			if (settled) return;
			settled = true;
			clearTimeout(timer);
			signal.removeEventListener("abort", abort);
			callback();
		};
		const abort = () => {
			child.kill("SIGTERM");
			finish(() => reject(new Error("xiaoqing_bridge_aborted")));
		};
		const timer = setTimeout(() => {
			child.kill("SIGTERM");
			finish(() => reject(new Error("xiaoqing_bridge_timeout")));
		}, XIAOQING_BRIDGE_TIMEOUT_MS);
		signal.addEventListener("abort", abort, { once: true });
		child.on("error", (error) => finish(() => reject(error)));
		child.stdout.on("data", (chunk: Buffer) => {
			stdoutBytes += chunk.length;
			if (stdoutBytes > MAX_OUTPUT_BYTES) {
				child.kill("SIGTERM");
				finish(() => reject(new Error("xiaoqing_bridge_output_too_large")));
				return;
			}
			stdout.push(chunk);
		});
		child.stderr.on("data", (chunk: Buffer) => {
			stderrBytes += chunk.length;
			if (stderrBytes <= MAX_OUTPUT_BYTES) stderr.push(chunk);
		});
		child.on("close", (code) => {
			finish(() => {
				if (code !== 0) {
					reject(new Error(boundedText(Buffer.concat(stderr).toString("utf8")) || "xiaoqing_bridge_failed"));
					return;
				}
				try {
					const response = JSON.parse(Buffer.concat(stdout).toString("utf8")) as MemoryBridgeResponse;
					if (
						response.ok !== true ||
						response.tool !== tool ||
						(response.effect !== "read" && response.effect !== "write")
					) throw new Error("xiaoqing_bridge_response_invalid");
					resolve(response);
				} catch (error) {
					reject(error instanceof Error ? error : new Error("xiaoqing_bridge_response_invalid"));
				}
			});
		});
		child.stdin.end(request);
	});
}

async function callDingtalkImageBridge(
	args: Record<string, unknown>,
	signal: AbortSignal,
): Promise<MemoryBridgeResponse> {
	const python = process.env.CEO_PI_PYTHON_BINARY?.trim();
	const bridge = process.env.CEO_PI_DINGTALK_IMAGE_BRIDGE_PATH?.trim();
	if (!python || !bridge || !path.isAbsolute(bridge)) throw new Error("dingtalk_image_bridge_not_configured");
	const request = JSON.stringify({ tool: "download_dingtalk_image", arguments: args });
	if (Buffer.byteLength(request, "utf8") > MAX_DINGTALK_IMAGE_REQUEST_BYTES) {
		throw new Error("dingtalk_image_request_too_large");
	}
	return await new Promise<MemoryBridgeResponse>((resolve, reject) => {
		const child = spawn(python, [bridge], {
			env: safeDingtalkImageEnvironment(),
			stdio: ["pipe", "pipe", "pipe"],
		});
		const stdout: Buffer[] = [];
		const stderr: Buffer[] = [];
		let stdoutBytes = 0;
		let stderrBytes = 0;
		let settled = false;
		const finish = (callback: () => void) => {
			if (settled) return;
			settled = true;
			clearTimeout(timer);
			signal.removeEventListener("abort", abort);
			callback();
		};
		const abort = () => {
			child.kill("SIGTERM");
			finish(() => reject(new Error("dingtalk_image_bridge_aborted")));
		};
		const timer = setTimeout(() => {
			child.kill("SIGTERM");
			finish(() => reject(new Error("dingtalk_image_bridge_timeout")));
		}, DINGTALK_IMAGE_BRIDGE_TIMEOUT_MS);
		signal.addEventListener("abort", abort, { once: true });
		child.on("error", (error) => finish(() => reject(error)));
		child.stdout.on("data", (chunk: Buffer) => {
			stdoutBytes += chunk.length;
			if (stdoutBytes > MAX_DINGTALK_IMAGE_RESPONSE_BYTES) {
				child.kill("SIGTERM");
				finish(() => reject(new Error("dingtalk_image_bridge_output_too_large")));
				return;
			}
			stdout.push(chunk);
		});
		child.stderr.on("data", (chunk: Buffer) => {
			stderrBytes += chunk.length;
			if (stderrBytes <= MAX_OUTPUT_BYTES) stderr.push(chunk);
		});
		child.on("close", (code) => {
			finish(() => {
				if (code !== 0) {
					reject(new Error(boundedText(Buffer.concat(stderr).toString("utf8")) || "dingtalk_image_bridge_failed"));
					return;
				}
				try {
					const response = JSON.parse(Buffer.concat(stdout).toString("utf8")) as MemoryBridgeResponse;
					if (
						response.ok !== true ||
						response.tool !== "download_dingtalk_image" ||
						response.effect !== "read"
					) throw new Error("dingtalk_image_bridge_response_invalid");
					resolve(response);
				} catch (error) {
					reject(error instanceof Error ? error : new Error("dingtalk_image_bridge_response_invalid"));
				}
			});
		});
		child.stdin.end(request);
	});
}

async function executeMemoryTool(
	tool: string,
	args: Record<string, unknown>,
	effect: "read" | "write",
	signal: AbortSignal,
) {
	const response = await callMemoryBridge(tool, args, signal);
	const responseText = boundedText(JSON.stringify(response.result));
	const targets = structuredTargetIdentifiers(args);
	const confirmed = effect === "write" && response.confirmed === true && Object.keys(response.receipt).length > 0;
	return {
		content: [{ type: "text" as const, text: responseText }],
		details: {
			protocolVersion: RECEIPT_PROTOCOL_VERSION,
			bridge: "memory_connector",
			effect,
			operation: tool,
			operationDigest: memoryOperationDigest(tool, args),
			targetIdentifiers: targets,
			resultDigest: createHash("sha256").update(responseText).digest("hex"),
			exitCode: 0,
			completed: true,
			safeToConfirm: confirmed,
			receipt: response.receipt,
		},
	};
}

async function executeExaTool(
	tool: string,
	args: Record<string, unknown>,
	signal: AbortSignal,
) {
	const response = await callExaBridge(tool, args, signal);
	const responseText = boundedText(JSON.stringify(response.result));
	return {
		content: [{ type: "text" as const, text: responseText }],
		details: {
			protocolVersion: RECEIPT_PROTOCOL_VERSION,
			bridge: "exa",
			effect: "read",
			operation: tool,
			operationDigest: memoryOperationDigest(tool, args),
			targetIdentifiers: structuredTargetIdentifiers(args),
			resultDigest: createHash("sha256").update(responseText).digest("hex"),
			exitCode: 0,
			completed: true,
			safeToConfirm: false,
			receipt: {},
		},
	};
}

async function executeXiaoqingTool(
	tool: string,
	args: Record<string, unknown>,
	signal: AbortSignal,
) {
	const response = await callXiaoqingBridge(tool, args, signal);
	const responseText = boundedText(JSON.stringify(response.result));
	const effect = response.effect ?? "read";
	const confirmed = effect === "write" && response.confirmed === true && Object.keys(response.receipt).length > 0;
	return {
		content: [{ type: "text" as const, text: responseText }],
		details: {
			protocolVersion: RECEIPT_PROTOCOL_VERSION,
			bridge: "xiaoqing_interview",
			effect,
			operation: tool,
			operationDigest: memoryOperationDigest(tool, args),
			targetIdentifiers: structuredTargetIdentifiers(args),
			resultDigest: createHash("sha256").update(responseText).digest("hex"),
			exitCode: 0,
			completed: true,
			safeToConfirm: confirmed,
			receipt: response.receipt,
		},
	};
}

async function executeDingtalkImageTool(
	args: Record<string, unknown>,
	signal: AbortSignal,
) {
	const response = await callDingtalkImageBridge(args, signal);
	const result = response.result as Record<string, unknown>;
	const data = result?.data;
	const mimeType = result?.mime_type;
	const byteLength = result?.byte_length;
	if (
		typeof data !== "string" ||
		!data ||
		typeof mimeType !== "string" ||
		!new Set(["image/png", "image/jpeg", "image/gif", "image/webp"]).has(mimeType) ||
		typeof byteLength !== "number" ||
		!Number.isInteger(byteLength) ||
		byteLength <= 0 ||
		byteLength > MAX_REVIEWED_IMAGE_BYTES
	) throw new Error("dingtalk_image_bridge_response_invalid");
	const decoded = Buffer.from(data, "base64");
	if (decoded.length !== byteLength || reviewedImageMimeType(decoded) !== mimeType) {
		throw new Error("dingtalk_image_bridge_response_invalid");
	}
	const responseText = JSON.stringify({ imageAttached: true, mimeType, byteLength });
	return {
		content: [
			{ type: "text" as const, text: responseText },
			{ type: "image" as const, data, mimeType },
		],
		details: {
			protocolVersion: RECEIPT_PROTOCOL_VERSION,
			bridge: "dingtalk_image",
			effect: "read",
			operation: "download_dingtalk_image",
			operationDigest: memoryOperationDigest("download_dingtalk_image", args),
			targetIdentifiers: {},
			resultDigest: createHash("sha256").update(decoded).digest("hex"),
			exitCode: 0,
			completed: true,
			safeToConfirm: false,
			receipt: {},
			imageAttached: true,
			imageMimeType: mimeType,
			imageByteLength: byteLength,
		},
	};
}

const xiaoqingArguments = Type.Record(
	Type.String({ minLength: 1, maxLength: 256 }),
	Type.Unknown(),
);

function assertReviewedCommand(argv: string[], metadata: DwsToolMetadata, expected: "read" | "write"): void {
	const commandSegments = metadata.cli_path.split(/\s+/).map((segment) => segment.toLowerCase());
	if (commandSegments.some((segment) => BLOCKED_COMMAND_SEGMENTS.has(segment))) {
		throw new Error("reviewed_auth_or_install_command_forbidden");
	}
	if (metadata.availability && metadata.availability !== "available") {
		throw new Error("reviewed_command_unavailable");
	}
	if (metadata.confirmation === "user_required") {
		throw new Error("reviewed_user_confirmation_required");
	}
	if (expected === "read" && metadata.effect !== "read") {
		throw new Error("reviewed_read_requires_read_effect");
	}
	if (expected === "write" && metadata.effect !== "write") {
		throw new Error(
			metadata.effect === "destructive" ? "reviewed_destructive_command_forbidden" : "reviewed_write_requires_write_effect",
		);
	}
	if (argv.includes("--dry-run")) throw new Error("reviewed_dry_run_is_not_execution_evidence");
}

function assertReviewedLarkCommand(
	argv: string[],
	metadata: LarkToolMetadata,
	expected: "read" | "write",
): void {
	const commandSegments = metadata.name.split(/\s+/).map((segment) => segment.toLowerCase());
	if (
		commandSegments.some((segment) => BLOCKED_COMMAND_SEGMENTS.has(segment)) ||
		new Set(["auth", "config", "profile", "update"]).has(commandSegments[0])
	) throw new Error("reviewed_auth_or_install_command_forbidden");
	const risk = metadata._meta?.risk;
	if (risk === "high-risk-write" || metadata._meta?.danger === true) {
		throw new Error("reviewed_destructive_command_forbidden");
	}
	if (expected === "read" && risk !== "read") {
		throw new Error("reviewed_read_requires_read_effect");
	}
	if (expected === "write" && risk !== "write") {
		throw new Error("reviewed_write_requires_write_effect");
	}
	if (argv.includes("--dry-run")) throw new Error("reviewed_dry_run_is_not_execution_evidence");
}

async function executeReviewedDws(argv: string[], effect: "read" | "write", signal: AbortSignal) {
	validateArgv(argv);
	const tools = await loadDwsMetadata();
	const metadata = commandMetadata(argv, tools);
	if (!metadata) throw new Error("reviewed_dws_command_unknown");
	assertReviewedCommand(argv, metadata, effect);
	const originalArgv = [...argv];
	const imageDownload = effect === "read" && metadata.cli_path === "chat message download-media";
	let imageTempDir: string | undefined;
	let imagePath: string | undefined;
	let executionArgv = originalArgv;
	if (imageDownload) {
		imageTempDir = await fs.mkdtemp(path.join(os.tmpdir(), "ceo-agent-pi-image-"));
		imagePath = path.join(imageTempDir, "downloaded-image");
		executionArgv = replaceOutputPath(originalArgv, imagePath);
	}
	try {
		const result = await execFileAsync(executionArgv[0], executionArgv.slice(1), {
			env: safeChildEnvironment(),
			timeout: COMMAND_TIMEOUT_MS,
			maxBuffer: MAX_OUTPUT_BYTES,
			signal,
		});
		const stdout = boundedText(result.stdout || "");
		const stderr = boundedText(result.stderr || "");
		let responseText = stdout || stderr || "{}";
		let content: Array<
			{ type: "text"; text: string } |
			{ type: "image"; data: string; mimeType: string }
		> = [{ type: "text", text: responseText }];
		let resultDigest = createHash("sha256").update(responseText).digest("hex");
		let imageDetails: Record<string, unknown> = {};
		if (imagePath) {
			const image = await readReviewedImage(imagePath);
			responseText = JSON.stringify({
				imageAttached: true,
				mimeType: image.content.mimeType,
				byteLength: image.byteLength,
			});
			content = [{ type: "text", text: responseText }, image.content];
			resultDigest = image.digest;
			imageDetails = {
				imageAttached: true,
				imageMimeType: image.content.mimeType,
				imageByteLength: image.byteLength,
			};
		}
		return {
			content,
			details: {
				protocolVersion: RECEIPT_PROTOCOL_VERSION,
				cli: "dws",
				effect,
				operation: metadata.cli_path,
				operationDigest: commandDigest(originalArgv),
				targetIdentifiers: targetIdentifiers(originalArgv),
				resultDigest,
				exitCode: 0,
				completed: true,
				safeToConfirm: effect === "write",
				stderr: stderr || undefined,
				...imageDetails,
			},
		};
	} finally {
		if (imageTempDir) await fs.rm(imageTempDir, { recursive: true, force: true });
	}
}

async function executeReviewedLark(argv: string[], effect: "read" | "write", signal: AbortSignal) {
	validateLarkArgv(argv);
	const tools = await loadLarkMetadata();
	const metadata = larkCommandMetadata(argv, tools) ?? await loadLarkShortcutMetadata(argv);
	if (!metadata) throw new Error("reviewed_lark_command_unknown");
	assertReviewedLarkCommand(argv, metadata, effect);
	const result = await execFileAsync(configuredLarkBinary(), argv.slice(1), {
		env: safeChildEnvironment(),
		timeout: COMMAND_TIMEOUT_MS,
		maxBuffer: MAX_OUTPUT_BYTES,
		signal,
	});
	const stdout = boundedText(result.stdout || "");
	const stderr = boundedText(result.stderr || "");
	const responseText = stdout || stderr || "{}";
	let resultIdentifiers: Record<string, string> = {};
	if (stdout) {
		try {
			resultIdentifiers = structuredTargetIdentifiers(JSON.parse(stdout));
		} catch {
			resultIdentifiers = {};
		}
	}
	const safeToConfirm = effect === "write" && Object.keys(resultIdentifiers).length > 0;
	return {
		content: [{ type: "text" as const, text: responseText }],
		details: {
			protocolVersion: RECEIPT_PROTOCOL_VERSION,
			cli: "lark-cli",
			effect,
			operation: metadata.name,
			operationDigest: commandDigest(argv),
			targetIdentifiers: targetIdentifiers(argv),
			resultDigest: createHash("sha256").update(responseText).digest("hex"),
			exitCode: 0,
			completed: true,
			safeToConfirm,
			receipt: safeToConfirm ? { resultIdentifiers, processingStatus: "completed" } : {},
			stderr: stderr || undefined,
		},
	};
}

async function executeReviewedGraphify(
	params: { operation: "query" | "explain" | "path"; query?: string; source?: string; target?: string },
	signal: AbortSignal,
	cwd: string,
) {
	let argv: string[];
	if (params.operation === "query" || params.operation === "explain") {
		if (params.source !== undefined || params.target !== undefined) {
			throw new Error("reviewed_graphify_arguments_invalid");
		}
		argv = [params.operation, validateGraphifyValue(params.query ?? "")];
	} else if (params.operation === "path") {
		if (params.query !== undefined) throw new Error("reviewed_graphify_arguments_invalid");
		argv = [
			"path",
			validateGraphifyValue(params.source ?? ""),
			validateGraphifyValue(params.target ?? ""),
		];
	} else {
		throw new Error("reviewed_graphify_operation_invalid");
	}
	const binary = configuredGraphifyBinary();
	const result = await execFileAsync(binary, argv, {
		env: safeChildEnvironment(),
		cwd,
		timeout: COMMAND_TIMEOUT_MS,
		maxBuffer: MAX_OUTPUT_BYTES,
		signal,
	});
	const stdout = boundedText(result.stdout || "");
	const stderr = boundedText(result.stderr || "");
	const responseText = stdout || stderr || "{}";
	const command = ["graphify", ...argv];
	return {
		content: [{ type: "text" as const, text: responseText }],
		details: {
			protocolVersion: RECEIPT_PROTOCOL_VERSION,
			cli: "graphify",
			effect: "read",
			operation: `graphify ${params.operation}`,
			operationDigest: commandDigest(command),
			targetIdentifiers: {},
			resultDigest: createHash("sha256").update(responseText).digest("hex"),
			exitCode: 0,
			completed: true,
			safeToConfirm: false,
			receipt: {},
			stderr: stderr || undefined,
		},
	};
}

export default function ceoAgentTools(pi: ExtensionAPI) {
	pi.registerTool({
		name: "workspace_read",
		label: "Workspace Read",
		description: "Read a UTF-8 text file inside the reviewed workspace or skill roots. Secret and unrelated paths are inaccessible.",
		parameters: Type.Object({
			path: Type.String({ minLength: 1 }),
			startLine: Type.Optional(Type.Integer({ minimum: 1 })),
			maxLines: Type.Optional(Type.Integer({ minimum: 1, maximum: 4000 })),
		}),
		async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
			const filePath = await resolveAllowedPath(params.path, ctx.cwd, { fileOnly: true });
			const stat = await fs.stat(filePath);
			if (stat.size > MAX_FILE_BYTES) throw new Error("workspace_file_too_large");
			const text = await fs.readFile(filePath, "utf8");
			const lines = text.split(/\r?\n/);
			const start = Math.max(0, (params.startLine ?? 1) - 1);
			const maximum = params.maxLines ?? 400;
			return {
				content: [{ type: "text", text: boundedText(lines.slice(start, start + maximum).join("\n")) }],
				details: { effect: "read", path: filePath, startLine: start + 1, maxLines: maximum },
			};
		},
	});

	pi.registerTool({
		name: "workspace_list",
		label: "Workspace List",
		description: "List entries in a reviewed workspace or skill directory without following paths outside the allowlist.",
		parameters: Type.Object({
			path: Type.String({ minLength: 1 }),
			maxEntries: Type.Optional(Type.Integer({ minimum: 1, maximum: 1000 })),
		}),
		async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
			const directory = await resolveAllowedPath(params.path, ctx.cwd);
			const entries = await fs.readdir(directory, { withFileTypes: true });
			const maximum = params.maxEntries ?? 200;
			const rendered = entries
				.slice(0, maximum)
				.map((entry) => `${entry.isDirectory() ? "directory" : entry.isFile() ? "file" : "other"}\t${entry.name}`)
				.join("\n");
			return {
				content: [{ type: "text", text: rendered }],
				details: { effect: "read", path: directory, count: Math.min(entries.length, maximum) },
			};
		},
	});

	pi.registerTool({
		name: "workspace_search",
		label: "Workspace Search",
		description: "Search text with ripgrep inside one reviewed root. No shell syntax is accepted.",
		parameters: Type.Object({
			query: Type.String({ minLength: 1, maxLength: 4096 }),
			path: Type.String({ minLength: 1 }),
			glob: Type.Optional(Type.String({ maxLength: 512 })),
			maxResults: Type.Optional(Type.Integer({ minimum: 1, maximum: MAX_SEARCH_RESULTS })),
		}),
		async execute(_toolCallId, params, signal, _onUpdate, ctx) {
			const searchPath = await resolveAllowedPath(params.path, ctx.cwd);
			const args = ["--line-number", "--color", "never", "--max-count", String(params.maxResults ?? 100)];
			if (params.glob) args.push("--glob", params.glob);
			args.push("--", params.query, searchPath);
			try {
				const result = await execFileAsync("rg", args, {
					env: safeChildEnvironment(),
					timeout: 30_000,
					maxBuffer: MAX_OUTPUT_BYTES,
					signal,
				});
				return { content: [{ type: "text", text: boundedText(result.stdout) }], details: { effect: "read" } };
			} catch (error: unknown) {
				const candidate = error as { code?: number | string; stdout?: string };
				if (candidate.code === 1) {
					return { content: [{ type: "text", text: candidate.stdout || "" }], details: { effect: "read", matches: 0 } };
				}
				throw error;
			}
		},
	});

	pi.registerTool({
		name: "write_work_profile",
		label: "Write Reviewed Work Profile",
		description: "Atomically replace only the configured work_profile.md during an explicit Nvwa profile-distillation run. No path argument or other file write is accepted.",
		parameters: Type.Object({
			markdown: Type.String({ minLength: 1, maxLength: MAX_WORK_PROFILE_BYTES }),
		}),
		async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
			const configured = process.env.CEO_PI_WORK_PROFILE_PATH?.trim();
			if (!configured || !path.isAbsolute(configured)) throw new Error("work_profile_path_not_configured");
			const encoded = Buffer.from(params.markdown, "utf8");
			if (encoded.length > MAX_WORK_PROFILE_BYTES) throw new Error("work_profile_too_large");
			if (params.markdown.includes("\0")) throw new Error("work_profile_invalid");
			const target = await resolveAllowedPath(configured, ctx.cwd, { mustExist: false });
			if (target !== path.resolve(configured)) throw new Error("work_profile_symlink_forbidden");
			try {
				const stat = await fs.lstat(target);
				if (stat.isSymbolicLink() || !stat.isFile()) throw new Error("work_profile_target_invalid");
			} catch (error) {
				const code = (error as NodeJS.ErrnoException).code;
				if (code !== "ENOENT") throw error;
			}
			await fs.mkdir(path.dirname(target), { recursive: true, mode: 0o700 });
			const temporary = path.join(
				path.dirname(target),
				`.${path.basename(target)}.pi-${process.pid}-${Date.now()}.tmp`,
			);
			try {
				await fs.writeFile(temporary, encoded, { flag: "wx", mode: 0o600 });
				await fs.rename(temporary, target);
			} catch (error) {
				await fs.unlink(temporary).catch(() => undefined);
				throw error;
			}
			const digest = createHash("sha256").update(encoded).digest("hex");
			return {
				content: [{ type: "text", text: "Reviewed work profile updated." }],
				details: {
					protocolVersion: RECEIPT_PROTOCOL_VERSION,
					effect: "write",
					operation: "write_work_profile",
					operationDigest: digest,
					targetIdentifiers: { artifact: "work_profile" },
					resultDigest: digest,
					exitCode: 0,
					completed: true,
					safeToConfirm: true,
					receipt: { artifact: "work_profile", sha256: digest },
				},
			};
		},
	});

	pi.registerTool({
		name: "execute_reviewed_read",
		label: "Reviewed DWS Read",
		description: "Execute one DWS command only when installed DWS schema metadata classifies the exact command as read-only.",
		parameters: Type.Object({ argv: Type.Array(Type.String(), { minItems: 2, maxItems: MAX_ARG_COUNT }) }),
		async execute(_toolCallId, params, signal) {
			return executeReviewedDws(params.argv, "read", signal);
		},
	});

	pi.registerTool({
		name: "graphify_read",
		label: "Graphify Read",
		description: "Run one installed Graphify query, explain, or path operation. This adapter is permanently read-only and never exposes shell execution.",
		parameters: Type.Object({
			operation: Type.Union([Type.Literal("query"), Type.Literal("explain"), Type.Literal("path")]),
			query: Type.Optional(Type.String({ minLength: 1, maxLength: MAX_GRAPHIFY_VALUE_BYTES })),
			source: Type.Optional(Type.String({ minLength: 1, maxLength: MAX_GRAPHIFY_VALUE_BYTES })),
			target: Type.Optional(Type.String({ minLength: 1, maxLength: MAX_GRAPHIFY_VALUE_BYTES })),
		}),
		async execute(_toolCallId, params, signal, _onUpdate, ctx) {
			return executeReviewedGraphify(params, signal, ctx.cwd);
		},
	});

	pi.registerTool({
		name: "download_dingtalk_image",
		label: "DingTalk Image Download",
		description: "Resolve one exact DingTalk robot image download code through the configured DWS identity and return the image pixels without exposing its signed URL.",
		parameters: Type.Object({
			download_code: Type.String({ minLength: 1, maxLength: 4096 }),
		}),
		async execute(_toolCallId, params, signal) {
			return executeDingtalkImageTool(params, signal);
		},
	});

	pi.registerTool({
		name: "execute_reviewed_write",
		label: "Reviewed DWS Write",
		description: "Execute one non-destructive DWS write whose installed schema metadata explicitly marks it as write. Destructive and authentication commands are rejected.",
		parameters: Type.Object({ argv: Type.Array(Type.String(), { minItems: 2, maxItems: MAX_ARG_COUNT }) }),
		async execute(_toolCallId, params, signal) {
			return executeReviewedDws(params.argv, "write", signal);
		},
	});

	pi.registerTool({
		name: "execute_reviewed_lark_read",
		label: "Reviewed Lark Read",
		description: "Execute one lark-cli command only when the installed official schema or shortcut help classifies it as read-only. Authentication and configuration commands are rejected.",
		parameters: Type.Object({ argv: Type.Array(Type.String(), { minItems: 2, maxItems: MAX_ARG_COUNT }) }),
		async execute(_toolCallId, params, signal) {
			return executeReviewedLark(params.argv, "read", signal);
		},
	});

	pi.registerTool({
		name: "execute_reviewed_lark_write",
		label: "Reviewed Lark Write",
		description: "Execute one non-destructive lark-cli write classified as write by official metadata. High-risk writes, authentication, configuration, and update commands are rejected; success requires a structured resource receipt.",
		parameters: Type.Object({ argv: Type.Array(Type.String(), { minItems: 2, maxItems: MAX_ARG_COUNT }) }),
		async execute(_toolCallId, params, signal) {
			return executeReviewedLark(params.argv, "write", signal);
		},
	});

	pi.registerTool({
		name: "web_search_exa",
		label: "Exa Web Search",
		description: "Search the public web through the reviewed Exa MCP bridge. This tool is read-only and accepts one bounded natural-language query.",
		parameters: Type.Object({
			query: Type.String({ minLength: 1, maxLength: 8192 }),
			numResults: Type.Optional(Type.Integer({ minimum: 1, maximum: 10 })),
		}),
		async execute(_toolCallId, params, signal) {
			return executeExaTool("web_search_exa", params, signal);
		},
	});

	pi.registerTool({
		name: "web_fetch_exa",
		label: "Exa Web Fetch",
		description: "Fetch public HTTP(S) pages through the reviewed Exa MCP bridge. Local, private, credentialed, and metadata URLs are rejected.",
		parameters: Type.Object({
			urls: Type.Array(Type.String({ minLength: 1, maxLength: 4096 }), { minItems: 1, maxItems: 10 }),
			maxCharacters: Type.Optional(Type.Integer({ minimum: 1, maximum: 20_000 })),
		}),
		async execute(_toolCallId, params, signal) {
			return executeExaTool("web_fetch_exa", params, signal);
		},
	});

	for (const [name, label, description] of [
		["search_candidates", "Xiaoqing Candidate Search", "Search Xiaoqing candidates using the exact arguments accepted by the authenticated Xiaoqing MCP schema."],
		["get_dashboard_stats", "Xiaoqing Dashboard Stats", "Read Xiaoqing interview dashboard statistics."],
		["get_interview_context", "Xiaoqing Interview Context", "Read the reviewed interview context for an exact candidate or interview identifier."],
		["download_attachment", "Xiaoqing Attachment", "Read or download one Xiaoqing attachment through the reviewed MCP bridge."],
		["list_candidate_interviews", "Xiaoqing Candidate Interviews", "List interviews for an exact Xiaoqing candidate."],
	] as const) {
		pi.registerTool({
			name,
			label,
			description: `${description} Put the native MCP fields inside the arguments object.`,
			parameters: Type.Object({ arguments: xiaoqingArguments }),
			async execute(_toolCallId, params, signal) {
				return executeXiaoqingTool(name, params.arguments, signal);
			},
		});
	}

	pi.registerTool({
		name: "upload_interview_result",
		label: "Xiaoqing Upload Interview Result",
		description: "Upload one interview result through the reviewed Xiaoqing MCP bridge. Put native MCP fields inside arguments; dry_run=true is read-only, while a real write is confirmable only with a completed record receipt.",
		parameters: Type.Object({ arguments: xiaoqingArguments }),
		async execute(_toolCallId, params, signal) {
			return executeXiaoqingTool("upload_interview_result", params.arguments, signal);
		},
	});

	pi.registerTool({
		name: "user_get",
		label: "Memory User Profile",
		description: "Read the authenticated Friday Memory user profile. Never accepts user_id, graph_id, or graph_ids.",
		parameters: Type.Object({ query: Type.Optional(Type.String({ maxLength: 4096 })) }),
		async execute(_toolCallId, params, signal) {
			return executeMemoryTool("user_get", params, "read", signal);
		},
	});

	pi.registerTool({
		name: "memory_recall",
		label: "Memory Recall",
		description: "Recall durable history for one focused query under the authenticated identity. Do not provide user_id or graph scope.",
		parameters: Type.Object({ query: Type.String({ minLength: 1, maxLength: 8192 }) }),
		async execute(_toolCallId, params, signal) {
			return executeMemoryTool("memory_recall", params, "read", signal);
		},
	});

	pi.registerTool({
		name: "memory_get",
		label: "Memory Get",
		description: "Read one exact Memory object by a UUID already returned by trusted recall.",
		parameters: Type.Object({ uuid: Type.String({ minLength: 1, maxLength: 256 }) }),
		async execute(_toolCallId, params, signal) {
			return executeMemoryTool("memory_get", params, "read", signal);
		},
	});

	pi.registerTool({
		name: "timeline_get",
		label: "Memory Timeline",
		description: "Read one exact Memory timeline by a known thread_id.",
		parameters: Type.Object({ thread_id: Type.String({ minLength: 1, maxLength: 256 }) }),
		async execute(_toolCallId, params, signal) {
			return executeMemoryTool("timeline_get", params, "read", signal);
		},
	});

	pi.registerTool({
		name: "memory_write",
		label: "Memory Write",
		description: "Write one authorized durable memory using only data, type, and created_at. Identity and graph scope come from authenticated ACL.",
		parameters: Type.Object({
			data: Type.String({ minLength: 1, maxLength: 512 * 1024 }),
			type: Type.String({ minLength: 1, maxLength: 64 }),
			created_at: Type.String({ minLength: 1, maxLength: 128 }),
		}),
		async execute(_toolCallId, params, signal) {
			return executeMemoryTool("memory_write", params, "write", signal);
		},
	});

	pi.registerTool({
		name: "document_upload",
		label: "Memory Document Upload",
		description: "Upload one explicitly authorized document to Friday Memory. Never accepts user_id or graph scope.",
		parameters: Type.Object({
			filename: Type.String({ minLength: 1, maxLength: 512 }),
			content_base64: Type.String({ minLength: 1, maxLength: 14 * 1024 * 1024 }),
			mime_type: Type.Optional(Type.String({ maxLength: 256 })),
			uploaded_at: Type.Optional(Type.String({ maxLength: 128 })),
			ingest_mode: Type.Optional(Type.Union([Type.Literal("semantic"), Type.Literal("graph")])),
		}),
		async execute(_toolCallId, params, signal) {
			return executeMemoryTool("document_upload", params, "write", signal);
		},
	});
}
