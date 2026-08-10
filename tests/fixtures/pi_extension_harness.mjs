import { pathToFileURL } from "node:url";

const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
const request = JSON.parse(Buffer.concat(chunks).toString("utf8"));

try {
	const loader = await import(pathToFileURL(request.loaderPath).href);
	const loaded = await loader.loadExtensions([request.extensionPath], request.cwd);
	if (loaded.errors.length > 0 || loaded.extensions.length !== 1) {
		throw new Error(JSON.stringify(loaded.errors));
	}
	const registration = loaded.extensions[0].tools.get(request.toolName);
	if (!registration) throw new Error(`tool_not_registered:${request.toolName}`);
	const result = await registration.definition.execute(
		"test-call",
		request.params,
		new AbortController().signal,
		() => {},
		{ cwd: request.cwd },
	);
	process.stdout.write(JSON.stringify({ ok: true, result }));
} catch (error) {
	process.stdout.write(
		JSON.stringify({
			ok: false,
			error: error instanceof Error ? error.message : String(error),
		}),
	);
}
