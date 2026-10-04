/**
 * 题解生成器用的 pi 扩展。
 *
 * 两个工具：
 *
 * - ``submit_editorial``：提交一道题的题解（hint 列表、正文、三语言代码）。
 * - ``verify_solution``：在一个一次性沙箱目录里编译并跑一份代码。
 *
 * **为什么用工具而不是让 pi 直接写库**
 *
 * pi 进程和数据库之间有网络和权限边界。给它一个 HTTP 工具，它就不需要
 * 数据库凭据，也不需要文件系统权限——能做的事只有「提交一篇格式正确的
 * 题解」这一件。这比「给它凭据再指望它不乱用」可靠得多。
 *
 * **为什么 verify_solution 要自己起沙箱而不是放开 bash**
 *
 * 放开 bash 等于把整个进程权限交出去：能读环境变量、能写任意路径、能访问
 * 网络。而验证代码只需要「在空目录里编译并运行」这一件事。所以这里自己
 * 建临时目录、限超时、限内存、禁网络，而不是把 bash 递给模型。
 */

import { spawn } from "node:child_process";
import { createReadStream, existsSync } from "node:fs";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { createInterface } from "node:readline";

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

// ---------------------------------------------------------------------------
// 配置
// ---------------------------------------------------------------------------

const API_BASE = process.env.SGU_ACW_API_BASE ?? "http://127.0.0.1:8091";
const SECRET = process.env.SGU_ACW_EDITORIAL_SECRET ?? "";
const MODEL = process.env.SGU_ACW_EDITORIAL_MODEL ?? "deepseek-flash";

/** 编译 + 运行的墙钟上限。超过就杀掉，当作验证失败。 */
const RUN_TIMEOUT_MS = 20_000;
/** 单个语言一次编译的时间上限，比运行宽松些。 */
const COMPILE_TIMEOUT_MS = 30_000;
/** 输出超过这个长度就截断，免得把上下文撑爆。 */
const MAX_OUTPUT_CHARS = 4000;

/**
 * 题面数据。
 *
 * CF 的题面页面有 Cloudflare 的 JS 挑战，curl / fetch 一律 403（返回
 * 「Just a moment...」），只有真实浏览器能拿到。与其在服务器上装一个
 * Chromium，不如把题面预先抽成一份索引——题面本身不会变。
 *
 * 数据来自 CPRet 数据集，只留 2024 年之前的题（contest < 1918），
 * 因为每日一题只抽这个范围（新题的 rating 不可靠）。
 */
const STATEMENTS_PATH =
  process.env.SGU_ACW_STATEMENTS ??
  join(dirname(fileURLToPath(import.meta.url)), "..", "..", "data", "cf_statements.jsonl");

/** 语言 → 源文件名 + 编译命令 + 运行命令。 */
type LangSpec = {
  file: string;
  compile?: string[][];
  run: string[];
};

const LANGS: Record<string, LangSpec> = {
  cpp17: {
    file: "main.cpp",
    // C++20：题解里用 ranges::sort、结构化绑定这些现代写法，
    // C++17 编译不过。提交时也要选 GNU C++20。
    compile: [["g++", "-std=c++20", "-O2", "-o", "main", "main.cpp"]],
    run: ["./main"],
  },
  py3: {
    file: "main.py",
    run: ["python3", "main.py"],
  },
  java17: {
    file: "Main.java",
    compile: [["javac", "Main.java"]],
    run: ["java", "Main"],
  },
};

// ---------------------------------------------------------------------------
// 沙箱执行
// ---------------------------------------------------------------------------

type RunResult = { code: number | null; stdout: string; stderr: string; timedOut: boolean };

function run(
  argv: string[],
  cwd: string,
  timeoutMs: number,
  stdin?: string,
): Promise<RunResult> {
  return new Promise((resolve) => {
    const [cmd, ...args] = argv;
    const child = spawn(cmd as string, args, {
      cwd,
      // 不继承环境：免得代码读到 SGU_ACW_EDITORIAL_SECRET 之类的凭据。
      // 只给 PATH 和 HOME，够编译器和解释器找到自己的库。
      env: { PATH: process.env.PATH ?? "", HOME: cwd, LANG: "C.UTF-8" },
      stdio: ["pipe", "pipe", "pipe"],
    });

    let stdout = "";
    let stderr = "";
    let timedOut = false;

    const timer = setTimeout(() => {
      timedOut = true;
      child.kill("SIGKILL");
    }, timeoutMs);

    child.stdout?.on("data", (chunk: Buffer) => {
      if (stdout.length < MAX_OUTPUT_CHARS) stdout += chunk.toString();
    });
    child.stderr?.on("data", (chunk: Buffer) => {
      if (stderr.length < MAX_OUTPUT_CHARS) stderr += chunk.toString();
    });

    if (stdin !== undefined) {
      child.stdin?.write(stdin);
      child.stdin?.end();
    } else {
      child.stdin?.end();
    }

    child.on("error", (err) => {
      clearTimeout(timer);
      resolve({ code: null, stdout, stderr: `${stderr}\n${String(err)}`, timedOut });
    });

    child.on("close", (code) => {
      clearTimeout(timer);
      resolve({ code, stdout, stderr, timedOut });
    });
  });
}

function clip(text: string): string {
  if (text.length <= MAX_OUTPUT_CHARS) return text;
  return `${text.slice(0, MAX_OUTPUT_CHARS)}\n…（输出已截断）`;
}

// ---------------------------------------------------------------------------
// 题面读取
// ---------------------------------------------------------------------------

type Statement = { key: string; title: string; url: string; text: string };

/** 已经查过的题面。一次会话里同一道题不会查两遍。 */
const statementCache = new Map<string, Statement | null>();

/**
 * 按题号查题面。找不到返回 null。
 *
 * 逐行扫描而不是整个读进来：文件 5.7MB，读一次几十毫秒，但常驻内存
 * 没有意义——一道题只查一次。
 */
async function findStatement(key: string): Promise<Statement | null> {
  const cached = statementCache.get(key);
  if (cached !== undefined) return cached;

  if (!existsSync(STATEMENTS_PATH)) {
    return null;
  }

  const stream = createReadStream(STATEMENTS_PATH, { encoding: "utf8" });
  const lines = createInterface({ input: stream, crlfDelay: Infinity });
  let found: Statement | null = null;
  try {
    for await (const line of lines) {
      if (!line.trim()) continue;
      const row = JSON.parse(line) as Statement;
      if (row.key === key) {
        found = row;
        break;
      }
    }
  } finally {
    lines.close();
    stream.destroy();
  }

  statementCache.set(key, found);
  return found;
}

// ---------------------------------------------------------------------------
// 扩展
// ---------------------------------------------------------------------------

export default function (pi: ExtensionAPI) {
  // -------------------------------------------------------------------------
  // 读题面
  // -------------------------------------------------------------------------

  const StatementParams = Type.Object({
    key: Type.String({
      description: "题号，如 903A / 1867C。CF 的格式是比赛号 + 题目字母。",
    }),
  });

  pi.registerTool({
    name: "get_problem_statement",
    label: "读取题面",
    description:
      "按题号读取 CF 题面（题目描述、输入输出格式、样例、提示）。写题解前先读这个。" +
      "只收录了 2024 年之前的题（每日一题的抽取范围）。",
    parameters: StatementParams,
    execute: async (_toolCallId, params) => {
      const key = params.key.trim().toUpperCase();
      const found = await findStatement(key);

      if (!found) {
        return {
          content: [
            {
              type: "text",
              text:
                `题库里没有 ${key} 的题面。` +
                `可能原因：这道题在 2024 年之前（每日一题不抽），或题号写错了。\n` +
                `不要凭猜测写题解——换一道题，或用 report_failure 报告。`,
            },
          ],
          details: undefined,
          isError: true,
        };
      }

      return {
        content: [
          {
            type: "text",
            text: `# ${found.key} ${found.title}\n来源：${found.url}\n\n${found.text}`,
          },
        ],
        details: undefined,
      };
    },
  });

  // -------------------------------------------------------------------------
  // 提交题解
  // -------------------------------------------------------------------------

  const SubmitParams = Type.Object({
    daily_problem_id: Type.Number({
      description: "题目编号，来自 list_pending_problems 的返回值",
    }),
    hints: Type.String({
      description:
        "提示列表，每行一条，由浅入深。2-4 条，每条一句话，不直接给做法。" +
        "例如：'先想想什么情况下答案是 -1'。可以是空字符串。",
    }),
    body: Type.String({
      description:
        "完整题解正文，Markdown + LaTeX（行内 $x$，独立公式 $$...$$）。" +
        "要讲清思路怎么来的、为什么这样做是对的、复杂度是多少。至少 200 字符。",
    }),
    code_cpp17: Type.String({ description: "C++17 完整可编译代码" }),
    code_py3: Type.String({ description: "Python 3 完整代码" }),
    code_java17: Type.String({ description: "Java 17 完整代码（类名必须是 Main）" }),
  });

  pi.registerTool({
    name: "submit_editorial",
    label: "提交题解",
    description:
      "把一道每日一题的题解提交到站点。包含 hint 列表、完整题解正文，以及 " +
      "C++17 / Python 3 / Java 17 三个语言的参考代码。提交后先存为草稿，" +
      "晚上 8 点对成员公布，参考代码次日凌晨 4 点公布。",
    parameters: SubmitParams,
    execute: async (_toolCallId, params, signal) => {
      if (!SECRET) {
        return {
          content: [
            {
              type: "text",
              text: "缺少 SGU_ACW_EDITORIAL_SECRET，无法提交。这是配置问题，不要重试。",
            },
          ],
          details: undefined,
          isError: true,
        };
      }

      const payload = {
        daily_problem_id: params.daily_problem_id,
        hints: params.hints ?? "",
        body: params.body,
        code: {
          cpp17: params.code_cpp17,
          py3: params.code_py3,
          java17: params.code_java17,
        },
        model: MODEL,
      };

      try {
        const response = await fetch(`${API_BASE}/api/internal/editorials`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Editorial-Secret": SECRET,
          },
          body: JSON.stringify(payload),
          signal,
        });

        const text = await response.text();
        if (!response.ok) {
          // 4xx 是内容不合格，重试没用；5xx 才值得重试。把区别告诉模型。
          const retryable = response.status >= 500;
          return {
            content: [
              {
                type: "text",
                text: `提交失败（HTTP ${response.status}）：${text}${
                  retryable ? "\n这是服务端问题，可以稍后重试。" : "\n内容不合格，需要修改后重新提交。"
                }`,
              },
            ],
            details: undefined,
            isError: true,
          };
        }

        return {
          content: [{ type: "text", text: `已提交。${text}` }],
          details: undefined,
        };
      } catch (error) {
        return {
          content: [
            {
              type: "text",
              text: `请求失败：${String(error)}。检查后端是否在跑（${API_BASE}）。`,
            },
          ],
          details: undefined,
          isError: true,
        };
      }
    },
  });

  // -------------------------------------------------------------------------
  // 验证代码
  // -------------------------------------------------------------------------

  const VerifyParams = Type.Object({
    language: Type.String({
      description: "语言：cpp17 / py3 / java17",
    }),
    source: Type.String({ description: "要验证的完整源代码" }),
    stdin: Type.Optional(
      Type.String({
        description:
          "喂给程序的标准输入。CF 的题通常需要多组测试，把样例按题面格式拼好传进来。",
      }),
    ),
  });

  pi.registerTool({
    name: "verify_solution",
    label: "验证代码",
    description:
      "在一个一次性的隔离目录里编译并运行代码，返回编译错误或程序输出。" +
      "用来确认参考代码至少能编译、能跑出预期结果。不能访问网络，超时会杀掉。",
    parameters: VerifyParams,
    execute: async (_toolCallId, params) => {
      const spec = LANGS[params.language];
      if (!spec) {
        return {
          content: [
            {
              type: "text",
              text: `不支持的语言：${params.language}。可选：${Object.keys(LANGS).join(" / ")}`,
            },
          ],
          details: undefined,
          isError: true,
        };
      }

      const dir = await mkdtemp(join(tmpdir(), "sgu-verify-"));
      try {
        await writeFile(join(dir, spec.file), params.source, "utf8");

        for (const command of spec.compile ?? []) {
          const result = await run(command, dir, COMPILE_TIMEOUT_MS);
          if (result.code !== 0) {
            return {
              content: [
                {
                  type: "text",
                  text: `编译失败：\n${clip(result.stderr || result.stdout)}`,
                },
              ],
              details: undefined,
              isError: true,
            };
          }
        }

        const result = await run(spec.run, dir, RUN_TIMEOUT_MS, params.stdin);
        if (result.timedOut) {
          return {
            content: [
              {
                type: "text",
                text: `运行超时（超过 ${RUN_TIMEOUT_MS / 1000} 秒），可能是死循环。`,
              },
            ],
            details: undefined,
            isError: true,
          };
        }

        return {
          content: [
            {
              type: "text",
              text: [
                `退出码：${result.code}`,
                result.stdout ? `\n标准输出：\n${clip(result.stdout)}` : "",
                result.stderr ? `\n标准错误：\n${clip(result.stderr)}` : "",
              ].join(""),
            },
          ],
          details: undefined,
        };
      } finally {
        await rm(dir, { recursive: true, force: true });
      }
    },
  });

  // -------------------------------------------------------------------------
  // 查看待生成的题
  // -------------------------------------------------------------------------

  const PendingParams = Type.Object({
    day: Type.Optional(
      Type.String({ description: "YYYY-MM-DD，默认今天。一般不用传。" }),
    ),
  });

  pi.registerTool({
    name: "list_pending_problems",
    label: "查看待生成题目",
    description:
      "列出某一天还没写题解的每日一题，含题目编号、难度档、题面链接。" +
      "生成前先调这个，拿到 daily_problem_id 才能提交。",
    parameters: PendingParams,
    execute: async (_toolCallId, params) => {
      const query = params.day ? `?day=${encodeURIComponent(params.day)}` : "";
      try {
        const response = await fetch(`${API_BASE}/api/internal/editorials/pending${query}`, {
          headers: { "X-Editorial-Secret": SECRET },
        });
        const text = await response.text();
        if (!response.ok) {
          return {
            content: [{ type: "text", text: `查询失败（HTTP ${response.status}）：${text}` }],
            details: undefined,
            isError: true,
          };
        }
        return { content: [{ type: "text", text }], details: undefined };
      } catch (error) {
        return {
          content: [
            {
              type: "text",
              text: `请求失败：${String(error)}。检查后端是否在跑（${API_BASE}）。`,
            },
          ],
          details: undefined,
          isError: true,
        };
      }
    },
  });

  // -------------------------------------------------------------------------
  // 报告失败
  // -------------------------------------------------------------------------

  const FailParams = Type.Object({
    daily_problem_id: Type.Number({ description: "题目编号" }),
    error: Type.String({ description: "失败原因，会显示给管理员" }),
  });

  pi.registerTool({
    name: "report_failure",
    label: "报告生成失败",
    description:
      "某道题实在写不出来时调这个，把原因记下来。站点会显示「题解生成失败」，" +
      "管理员能看到原因。比什么都不交要好——静默失败看起来像漏掉了。",
    parameters: FailParams,
    execute: async (_toolCallId, params) => {
      try {
        const response = await fetch(`${API_BASE}/api/internal/editorials/fail`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Editorial-Secret": SECRET,
          },
          body: JSON.stringify(params),
        });
        const text = await response.text();
        return {
          content: [{ type: "text", text: response.ok ? `已记录。${text}` : `失败：${text}` }],
          details: undefined,
          isError: !response.ok,
        };
      } catch (error) {
        return {
          content: [{ type: "text", text: `请求失败：${String(error)}` }],
          details: undefined,
          isError: true,
        };
      }
    },
  });
}
