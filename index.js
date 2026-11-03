/**
 * dsh-plugin-finance-ocr — 财务图片核对插件（Host 半）。
 *
 * 把「拍照/扫描的财务单据图片」变成「可追溯、经过守恒校验的 Excel」。
 * 注册三个模型可见工具，全部走本机离线 OCR（RapidOCR + onnxruntime），不联网：
 *
 *   finance_ocr_extract   图片 -> extraction 文档（行/列/bbox/置信度/源图 SHA-256）
 *   finance_verify        extraction -> 校验报告（明细=合计 等硬校验，硬失败不放行）
 *   finance_export_xlsx   extraction + 校验 -> 4 sheet 工作簿（数据/校验报告/问题清单/运行清单）
 *
 * Python 侧唯一入口是 lib/cli.py，输入输出都走临时 JSON 文件（不依赖 stdin/stdout）。
 * Python 解释器查找顺序：DSH_FINANCE_PYTHON -> %DSH_HOME%\dsh-runtimes\*\dependencies\python -> PATH。
 *
 * @module dsh-plugin-finance-ocr
 */

import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { defineTool } from '@deepseek-ai/dsh-tools'

/** Cordis 插件名。 */
export const name = 'plugin-finance-ocr'

/** 只需要工具注册表。 */
export const inject = ['tools']

const HERE = path.dirname(fileURLToPath(import.meta.url))
const CLI_PATH = path.join(HERE, 'lib', 'cli.py')
const RUN_ROOT = path.join(os.tmpdir(), 'dsh-finance-ocr-run')
const DEFAULT_TIMEOUT_MS = 180_000
const LOG_TAIL_CHARS = 4_000

let runSeq = 0

/** 模型侧统一的文本输出声明。 */
const TEXT_OUTPUT = {
  schema: { type: 'string' },
  render: (_args, value) => [{ type: 'text', text: value }],
}

/** JSON 美化输出，任何值都能转。 */
function asText(value) {
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

/**
 * 找到可用的 Python 解释器。
 * @returns {string} python 可执行文件绝对路径
 * @throws 找不到时给出可操作的错误信息
 */
function findPython() {
  const fromEnv = process.env.DSH_FINANCE_PYTHON
  if (fromEnv && fs.existsSync(fromEnv)) return fromEnv

  const home = process.env.DSH_HOME || path.join(os.homedir(), '.dsh')
  const runtimes = path.join(home, 'dsh-runtimes')
  try {
    for (const runtime of fs.readdirSync(runtimes)) {
      const candidates = [
        path.join(runtimes, runtime, 'dependencies', 'python', 'python.exe'),
        path.join(runtimes, runtime, 'dependencies', 'python', 'bin', 'python3'),
      ]
      for (const candidate of candidates) {
        if (fs.existsSync(candidate)) return candidate
      }
    }
  } catch {
    // 没有 dsh-runtimes 时继续看 PATH
  }

  const pathDirs = (process.env.PATH || '').split(path.delimiter).filter(Boolean)
  const exeNames = process.platform === 'win32' ? ['python.exe', 'python3.exe', 'py.exe'] : ['python3', 'python']
  for (const dir of pathDirs) {
    for (const exe of exeNames) {
      const candidate = path.join(dir, exe)
      try {
        if (fs.existsSync(candidate) && fs.statSync(candidate).isFile()) return candidate
      } catch {
        // 忽略无权限的 PATH 项
      }
    }
  }

  throw new Error(
    '找不到 Python 解释器。请设置环境变量 DSH_FINANCE_PYTHON 指向 python.exe，' +
      '或确认 DSH 运行时目录（%DSH_HOME%\\dsh-runtimes）完整。',
  )
}

/** 读文件末尾若干字符，用于把 Python 的失败原因带回模型。 */
function readLogTail(logPath) {
  try {
    const text = fs.readFileSync(logPath, 'utf8')
    return text.length > LOG_TAIL_CHARS ? text.slice(-LOG_TAIL_CHARS) : text
  } catch {
    return ''
  }
}

/**
 * 调用 lib/cli.py 的一个子命令。
 * 输入输出都走临时文件：既不依赖 stdin/stdout，也避开 Windows 沙箱对命名管道的限制。
 * @param command - ocr_extract | verify | export_xlsx | selftest
 * @param payload - 输入 JSON 对象
 * @param options - timeoutMs 与调用方 signal
 * @returns Python 写出的 envelope（已解析的 JSON）
 */
async function callPython(command, payload, options = {}) {
  const python = findPython()
  fs.mkdirSync(RUN_ROOT, { recursive: true })
  const base = path.join(RUN_ROOT, `${process.pid}-${Date.now()}-${runSeq++}`)
  const inputPath = `${base}.in.json`
  const outputPath = `${base}.out.json`
  const logPath = `${base}.log`
  fs.writeFileSync(inputPath, JSON.stringify(payload), 'utf8')

  const logFd = fs.openSync(logPath, 'w')
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS
  const signal = options.signal

  let result
  let failureMessage
  try {
    result = await new Promise((resolve, reject) => {
      let timedOut = false
      let settled = false
      const finish = (fn, value) => {
        if (settled) return
        settled = true
        fn(value)
      }

      let child
      try {
        child = spawn(python, [CLI_PATH, command, '--input', inputPath, '--output', outputPath], {
          cwd: HERE,
          windowsHide: true,
          stdio: ['ignore', logFd, logFd],
          env: { ...process.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' },
        })
      } catch (error) {
        reject(new Error(`启动 Python 失败：${error?.message ?? String(error)}`))
        return
      }

      const timer = setTimeout(() => {
        timedOut = true
        try {
          child.kill()
        } catch {
          // 已经退出
        }
      }, timeoutMs)
      const onAbort = () => {
        try {
          child.kill()
        } catch {
          // 已经退出
        }
      }
      if (signal) {
        if (signal.aborted) onAbort()
        else signal.addEventListener('abort', onAbort, { once: true })
      }

      child.on('error', (error) => {
        clearTimeout(timer)
        reject(new Error(`启动 Python 失败：${error?.message ?? String(error)}`))
      })
      child.on('close', (code) => {
        clearTimeout(timer)
        if (signal) signal.removeEventListener('abort', onAbort)
        if (timedOut) {
          finish(
            reject,
            new Error(
              `识别/校验进程超过 ${Math.round(timeoutMs / 1000)} 秒被终止。` +
                `大图可先降采样再识别。\n${readLogTail(logPath)}`,
            ),
          )
          return
        }
        if (!fs.existsSync(outputPath)) {
          finish(
            reject,
            new Error(
              `Python 未写出结果文件（退出码 ${code}）。日志：\n${readLogTail(logPath) || '(空)'}`,
            ),
          )
          return
        }
        try {
          finish(resolve, JSON.parse(fs.readFileSync(outputPath, 'utf8')))
        } catch (error) {
          finish(reject, new Error(`结果 JSON 解析失败：${error?.message ?? String(error)}`))
        }
      })
    })
  } catch (error) {
    failureMessage = error
  } finally {
    try {
      fs.closeSync(logFd)
    } catch {
      // 已关闭
    }
    for (const file of [inputPath, outputPath, logPath]) {
      try {
        fs.unlinkSync(file)
      } catch {
        // 不存在
      }
    }
  }
  if (failureMessage) throw failureMessage
  return result
}

/** 每个工具共用的错误转文本。 */
function toolError(command, error) {
  return asText({
    ok: false,
    command,
    error: { code: 'TOOL_CALL_FAILED', message: error?.message ?? String(error) },
  })
}

/**
 * 注册 finance_* 工具。
 * @param ctx - 携带工具注册表的 Cordis 上下文
 */
export function apply(ctx) {
  ctx.tools.register(
    defineTool({
      name: 'finance_ocr_extract',
      description:
        '本地 OCR 识别一张财务单据图片（发票、报销单、记账凭证、台账截图、报表截图），' +
        '输出结构化表格：列映射、逐行字段值、每个单元格的原图坐标(box)、OCR 原文与置信度，' +
        '并记录源图 SHA-256。全程离线、不联网、不上传。' +
        '识别结果写入临时 extraction JSON 并返回其路径，供 finance_verify / finance_export_xlsx 继续使用。' +
        '注意：OCR 一定有错，任何金额都不要直接采信，必须接着调用 finance_verify。',
      parameters: {
        image: {
          type: 'string',
          required: true,
          description: '财务单据图片的绝对路径（png/jpg/jpeg/bmp/webp）。单张单据一张图效果最好。',
        },
        template: {
          type: 'string',
          description: '列映射模板名，默认 auto（按中文表头关键词自动识别列）。目前支持 auto。',
        },
        min_confidence: {
          type: 'number',
          description: '单元格最低置信度阈值 0-1，默认 0.5；低于该值的单元格会被标记出来，不会静默丢弃。',
        },
      },
      output: TEXT_OUTPUT,
      async execute(args, exec) {
        try {
          if (!args.image || !fs.existsSync(args.image)) {
            throw new Error(`图片不存在或不是绝对路径：${args.image ?? '(空)'}`)
          }
          const doc = await callPython(
            'ocr_extract',
            {
              image: args.image,
              template: args.template ?? 'auto',
              min_confidence: args.min_confidence ?? 0.5,
            },
            { signal: exec.signal },
          )
          return asText(doc)
        } catch (error) {
          return toolError('finance_ocr_extract', error)
        }
      },
    }),
  )

  ctx.tools.register(
    defineTool({
      name: 'finance_verify',
      description:
        '对 finance_ocr_extract 产出的 extraction 文档做财务守恒校验，不重新读图、不放行猜测。' +
        '硬校验包括：明细金额合计 = 票面合计、行数守恒（每个 OCR 行都有明确去向）、' +
        '金额可解析、长编号不被转科学计数法、源图哈希未变、重复行检测。' +
        '返回 status=pass / needs_review / fail，以及逐条 checks 和差异清单（差额、行号、原始识别文本）。' +
        'status=fail 时禁止继续导出；needs_review 表示差异需人工确认，不得自行定性。',
      parameters: {
        extraction: {
          type: 'string',
          required: true,
          description: 'finance_ocr_extract 返回的 extraction_path（extraction JSON 的绝对路径）。',
        },
        tolerance: {
          type: 'string',
          description: '金额比较容差（十进制字符串），默认 "0.01"。要最严格传 "0.00"。',
        },
        low_confidence: {
          type: 'number',
          description: '低于该置信度的单元格列入待人工确认，默认 0.90。',
        },
      },
      output: TEXT_OUTPUT,
      async execute(args, exec) {
        try {
          if (!args.extraction || !fs.existsSync(args.extraction)) {
            throw new Error(`extraction 文件不存在：${args.extraction ?? '(空)'}`)
          }
          const doc = await callPython(
            'verify',
            {
              extraction: args.extraction,
              tolerance: args.tolerance ?? '0.01',
              low_confidence: args.low_confidence ?? 0.9,
            },
            { signal: exec.signal },
          )
          return asText(doc)
        } catch (error) {
          return toolError('finance_verify', error)
        }
      },
    }),
  )

  ctx.tools.register(
    defineTool({
      name: 'finance_export_xlsx',
      description:
        '把 extraction 文档导出为 Excel 工作簿：数据（逐行字段 + 溯源列：原图路径/坐标/OCR 原文/置信度）、' +
        '校验报告、问题清单、运行清单（含输入 SHA-256）。导出前会重新跑 finance_verify；' +
        'status=fail 默认拒绝导出。输出路径不能等于源图路径或 extraction 文件路径。' +
        '金额用 Decimal 处理并写成数值格式，发票号码等长编号写成文本，防止 Excel 转科学计数法。',
      parameters: {
        extraction: {
          type: 'string',
          required: true,
          description: 'finance_ocr_extract 返回的 extraction_path。',
        },
        output: {
          type: 'string',
          required: true,
          description: '目标 xlsx 的绝对路径，父目录必须已存在。',
        },
        tolerance: {
          type: 'string',
          description: '金额比较容差（十进制字符串），默认 "0.01"。',
        },
        allow_unverified: {
          type: 'boolean',
          description:
            '仅在用户明确要求"先出表、差异待人工确认"时传 true，允许校验 fail 也导出；默认 false。',
        },
      },
      output: TEXT_OUTPUT,
      async execute(args, exec) {
        try {
          if (!args.extraction || !fs.existsSync(args.extraction)) {
            throw new Error(`extraction 文件不存在：${args.extraction ?? '(空)'}`)
          }
          if (!args.output) throw new Error('缺少 output')
          const doc = await callPython(
            'export_xlsx',
            {
              extraction: args.extraction,
              output: args.output,
              tolerance: args.tolerance ?? '0.01',
              allow_unverified: Boolean(args.allow_unverified),
            },
            { signal: exec.signal },
          )
          return asText(doc)
        } catch (error) {
          return toolError('finance_export_xlsx', error)
        }
      },
    }),
  )

  ctx.tools.register(
    defineTool({
      name: 'finance_batch_run',
      description:
        '批量处理一个文件夹里的多张财务单据图片：逐张执行「识别 → 校验 → 导出 xlsx」，' +
        '每张图各写一个工作簿到输出目录，并生成 batch-summary.json 汇总每张的状态、行数、硬失败代码与输出路径。' +
        '不同单据的金额**不合并**（合并会造出现实中不存在的汇总表），只做逐张处理与状态汇总。' +
        '每张图约 2~4 秒，图片多时耗时会成倍增长；一次最多默认 200 张。' +
        '适合用户给的是"一个文件夹的凭证/发票截图"而不是单张图时使用。',
      parameters: {
        directory: {
          type: 'string',
          required: true,
          description: '包含财务单据图片的文件夹绝对路径（只扫当前层，不递归）。',
        },
        output_dir: {
          type: 'string',
          description: 'xlsx 与 batch-summary.json 的输出目录绝对路径；默认是输入目录下的 finance-ocr-output。',
        },
        pattern: {
          type: 'string',
          description: '文件名过滤 glob，默认 "*"（全部支持的图片扩展名）。',
        },
        tolerance: {
          type: 'string',
          description: '金额比较容差（十进制字符串），默认 "0.01"。',
        },
        allow_unverified: {
          type: 'boolean',
          description: '仅当用户明确要求"先把所有图都出一遍表、差异待人工确认"时传 true；默认 false（校验 fail 的那张不导出）。',
        },
        max_files: {
          type: 'integer',
          description: '最多处理多少张，默认 200，上限 1000。',
        },
      },
      output: TEXT_OUTPUT,
      async execute(args, exec) {
        try {
          if (!args.directory || !fs.existsSync(args.directory)) {
            throw new Error(`目录不存在：${args.directory ?? '(空)'}`)
          }
          const doc = await callPython(
            'batch',
            {
              directory: args.directory,
              output_dir: args.output_dir ?? null,
              pattern: args.pattern ?? '*',
              tolerance: args.tolerance ?? '0.01',
              allow_unverified: Boolean(args.allow_unverified),
              max_files: args.max_files ?? 200,
            },
            { signal: exec.signal, timeoutMs: 900_000 },
          )
          return asText(doc)
        } catch (error) {
          return toolError('finance_batch_run', error)
        }
      },
    }),
  )

  ctx.tools.register(
    defineTool({
      name: 'finance_check_accommodation',
      description:
        '住宿费核对（施工住宿费明细表 vs 酒店流水）：按业务规则逐条核对，返回 pass/needs_review/fail 和逐条差异。' +
        '规则：①正主任可单住（副主任不能单住）；其他人同一天除主任外最多 1 间单住（凑不够），出现 2 间及以上单人间即不通过；' +
        '②同一天不能重复房间、不能一人住两间；③住宿天数=结束-开始，金额=天数×单价；' +
        '④住宿单价不得超上限（车间 130 元/人/天，机关 200 元/人/天）；' +
        '⑤入住人员必须与签字确认是同一组人；⑥小计/合计（含大写）必须正确；' +
        '⑦酒店流水必须逐房间、逐日期段匹配，支付金额一致。' +
        '可以直接给图片现场识别（detail_images + statement_image，自动去红章、自动合并跨页/多日期段），' +
        '也可以给已结构化的 dataset 只跑核对。' +
        '**机关待遇/不需要流水的部门**（如设计室）：传 no_statement=true，只核对明细表（表格无房间号，' +
        '双人间/流水规则自动跳过），可多张明细表一起核对。' +
        '同一批报销里如果住的是**不同酒店/不同住宿地点**，用 pairs 传多组，每组用自己的明细+流水核对后汇总。' +
        '手写签字无法机读，会进入待人工确认；人工核对过签字后可传 signature_verified=true。' +
        '被红章遮挡的流水房间号会用日期段+金额反查并提示人工确认。',
      parameters: {
        detail_images: {
          type: 'string',
          description: '明细表图片的绝对路径，1~2 页，多个用逗号分隔。给了 dataset 时可省略。',
        },
        statement_image: {
          type: 'string',
          description: '酒店流水图片的绝对路径。机关待遇/无流水（no_statement=true）时可不传；给了 dataset 时可省略。',
        },
        no_statement: {
          type: 'boolean',
          description: '机关待遇/不核对酒店流水：只读明细表（可多张），跳过流水匹配与双人间规则。',
        },
        signature_verified: {
          type: 'boolean',
          description: '签字已人工核对过（手写签字无法机读）：传 true 则签字规则记为已核对，不再提示待确认。',
        },
        dataset: {
          type: 'string',
          description: '已结构化的核对数据 JSON 绝对路径；提供后跳过图片识别、只跑核对。',
        },
        pairs: {
          type: 'string',
          description:
            '多住宿地点模式：JSON 数组字符串，每项 {label, detail_images, statement_image, save_dataset}。' +
            '例如 [{"label":"鑫海酒店","detail_images":["D:\\\\a\\\\明细1.png"],"statement_image":"D:\\\\a\\\\流水.png"}]。' +
            '同一批报销里不同住宿地点各一组，分别核对后汇总；提供 pairs 时忽略单组参数。',
        },
        year: {
          type: 'integer',
          description: '年份。默认从酒店流水的"打印日期"推断，推断不到用策略文件/2026。',
        },
        department: {
          type: 'string',
          description: '部门/车间名称（如"某段B车间"），用于确认车间主任名单的适用范围。',
        },
        directors: {
          type: 'string',
          description: '车间主任姓名，逗号分隔（单住例外名单）；不传时读取本机策略文件。',
        },
        rate: {
          type: 'string',
          description: '住宿单价，默认 128。用于校验天数×单价=金额。',
        },
        save_dataset: {
          type: 'string',
          description: '把识别出的结构化数据另存到该路径，便于人工修正后复跑核对。',
        },
        ledger: {
          type: 'string',
          description:
            '台账 JSONL 的绝对路径；不传时读取本机策略文件。提供后每次核对自动追加一行台账并刷新同名 xlsx。',
        },
        batch: {
          type: 'string',
          description: '批次名（如 "2026-10 第1批"）；不传时按最早住宿日期推断为 "YYYY-MM"。',
        },
        label: {
          type: 'string',
          description: '住宿地点名称（单组模式）；pairs 模式每组用自己的 label。',
        },
      },
      output: TEXT_OUTPUT,
      async execute(args, exec) {
        try {
          if (!args.pairs && !args.dataset) {
            const detailPaths = String(args.detail_images ?? '')
              .split(/[;,]/)
              .map((item) => item.trim())
              .filter(Boolean)
            if (detailPaths.length === 0) {
              throw new Error('需要 detail_images（明细表图片路径）、pairs（多住宿地点）或直接给 dataset')
            }
            for (const file of detailPaths) {
              if (!fs.existsSync(file)) throw new Error(`明细表图片不存在：${file}`)
            }
            if (!args.no_statement) {
              if (!args.statement_image || !fs.existsSync(args.statement_image)) {
                throw new Error(`酒店流水图片不存在：${args.statement_image ?? '(空)'}（机关待遇/无流水请传 no_statement=true）`)
              }
            }
          } else if (!args.pairs && args.dataset && !fs.existsSync(args.dataset)) {
            throw new Error(`dataset 文件不存在：${args.dataset}`)
          }
          const payload = {
            detail_images: args.detail_images,
            statement_image: args.statement_image,
            dataset: args.dataset,
            save_dataset: args.save_dataset,
            pairs: args.pairs,
            ledger: args.ledger,
            batch: args.batch,
            label: args.label,
          }
          if (args.no_statement === true) payload.no_statement = true
          if (args.signature_verified === true) payload.signature_verified = true
          if (args.year !== undefined) payload.year = args.year
          if (args.department) payload.department = args.department
          if (args.directors) payload.directors = args.directors
          if (args.rate) payload.rate = args.rate
          const doc = await callPython('accommodation', payload, {
            signal: exec.signal,
            timeoutMs: 900_000,
          })
          return asText(doc)
        } catch (error) {
          return toolError('finance_check_accommodation', error)
        }
      },
    }),
  )

  ctx.tools.register(
    defineTool({
      name: 'finance_ledger',
      description:
        '住宿费核对台账：把每次核对结果累积成一份可追溯的台账（JSONL 源 + 同名 xlsx 人看版）。' +
        'action=list 查看全部记录与合计；action=export 重新生成 xlsx；' +
        'action=archive 归档旧记录（保留最近 keep_last 条，其余移到 台账归档-<时间>.jsonl），避免台账无限膨胀。' +
        '台账路径默认读本机策略文件，可在参数里覆盖。',
      parameters: {
        action: {
          type: 'string',
          required: true,
          enum: ['list', 'export', 'archive'],
          description: 'list 查看 / export 导出 xlsx / archive 归档旧记录。',
        },
        ledger: {
          type: 'string',
          description: '台账 JSONL 的绝对路径；不传时读取本机策略文件。',
        },
        output: {
          type: 'string',
          description: 'action=export 时的 xlsx 输出路径；默认与台账同名 .xlsx。',
        },
        keep_last: {
          type: 'integer',
          description: 'action=archive 时保留最近多少条记录（其余归档）。',
        },
        before: {
          type: 'string',
          description: 'action=archive 时归档此日期（YYYY-MM-DD）之前的记录。',
        },
        archive_dir: {
          type: 'string',
          description: 'action=archive 时归档文件目录，默认台账所在目录。',
        },
      },
      output: TEXT_OUTPUT,
      async execute(args, exec) {
        try {
          const doc = await callPython(
            'ledger',
            {
              action: args.action,
              ledger: args.ledger,
              output: args.output,
              keep_last: args.keep_last,
              before: args.before,
              archive_dir: args.archive_dir,
            },
            { signal: exec.signal },
          )
          return asText(doc)
        } catch (error) {
          return toolError('finance_ledger', error)
        }
      },
    }),
  )
}
