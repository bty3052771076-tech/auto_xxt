# auto_xxt

使用 Python Playwright 清点超星课程小节，并按页面反馈处理视频、课件和可自动作答的测验。脚本会逐节保存进度；它不会绕过登录、任务点规则或人工批阅。

## 使用条件

- 仅操作自己有权访问的课程，并遵守课程及平台规则。运行完成脚本会播放视频、滚动课件、填写并提交测验；只想清点时使用 lesson_inventory.py。
- Windows PowerShell、Git、带 venv/pip 的 Python 3.10+、已安装的 Google Chrome，以及 Python 包 playwright。脚本使用系统 Chrome（channel=chrome），无需下载 Playwright 自带浏览器。
- 使用能打开目标课程的账号。登录由你在专用 Chrome 窗口中手动完成；脚本不读取密码。浏览器资料保存在项目的 .private/，不要与另一个脚本实例同时占用该资料目录。
- 课程页面可能需要带 enc 等临时参数的完整小节 URL。每次运行请传当前有效 URL，不要把它写入代码、README、提交记录或公开问题。
- 当前适配器按已测试的超星页面结构编写；不同课程或页面版本可能需要调整。选择/判断题可自动作答；填空、简答及待人工批阅题不能保证一次性完成。

## 快速使用（Windows PowerShell）

以下示例把虚拟环境建在 E 盘；可换成你自己的非 C 盘路径。安装依赖时不使用 pip 缓存。

~~~powershell
git clone https://github.com/bty3052771076-tech/auto_xxt.git E:\auto_xxt
Set-Location E:\auto_xxt
python -m venv E:\venvs\auto_xxt
$python = 'E:\venvs\auto_xxt\Scripts\python.exe'
& $python -m pip install --no-cache-dir playwright
~~~

打开自己的课程页面，取得当前完整的目录页或小节页 URL。通过提示输入，避免把带访问参数的 URL 写入命令历史。首次清点会打开专用 Chrome；若尚未登录，请在该窗口手动登录并进入课程。

~~~powershell
$courseUrl = Read-Host '粘贴当前课程的完整 URL'
$courseName = Read-Host '课程名称'
& $python .\scripts\lesson_inventory.py --url $courseUrl --course-name $courseName
~~~

清单写到 output/lesson-inventory/lesson-inventory.json。清点结束后，先关闭脚本打开的 Chrome 窗口，让该命令退出并释放浏览器资料目录。若登录或跳转后 URL 发生变化，下一步请重新输入当前有效的小节 URL。

~~~powershell
$courseUrl = Read-Host '粘贴当前有效的小节 URL'
& $python .\scripts\complete_course.py --url $courseUrl --course-name $courseName --assume-authenticated
~~~

脚本按清单顺序处理各节，逐节写入检查点。正常结束也会保留专用 Chrome 供查看；关闭该窗口后命令才会退出。随后可生成逐节汇总：

~~~powershell
& $python .\scripts\course_summary.py
~~~

请看汇总输出的完成节数和逐节状态；汇总命令本身成功退出不代表所有小节均已完成。

## 可选参数与答案文件

- 中断后用相同命令续跑，已有完成检查点会跳过；用 --restart 才会重查所选小节。
- 用 --section-id 指定一节，或用 --start-section-id / --stop-section-id 指定范围。
- 用 --skip-quizzes 只处理视频和课件，不提交测验；用 --close-on-finish 在处理后关闭专用 Chrome。
- 如果已有经过核对的私有答案 JSON，可添加 --answer-key 路径。文件须含 course_id 和 sections（小节 ID → 题目 ID → 答案）。页面正确答案与提供的键冲突时脚本会停止，不会强行提交。仅在页面显示本次成绩 100 分时才归档为已核验答案。
- 如果你已有私有的 output/course-run-data.json，它也符合 --answer-key 的顶层格式；该文件不随仓库提供，仓库内没有自动生成它的命令。现有完成脚本只读取其中顶层 sections 答案键，仍从 lesson-inventory.json 读取资源清单。

没有完整答案键时，脚本会对未开始的客观题先选择每题首项并提交一次以获取批阅答案，这会消耗提交次数；自动重做至少需要再有一次提交机会。若不希望脚本提交测验，请加 --skip-quizzes。

示例答案文件结构（真实答案文件不要提交到 Git）：

~~~json
{
  "course_id": "课程ID",
  "sections": {
    "小节ID": {
      "题目ID": "B"
    }
  }
}
~~~

## 完成判定与限制

可拖动视频先尝试定位末尾，再核对平台任务点；页面标记不可拖拽或要求观看时长的视频按正常 1× 速度播放至任务点确认。课件会滚动到底部。测验的“已提交”不等于“100 分”：待人工批阅、缺少可信答案或出现不支持的填空/简答题时，脚本会保留未完成状态并返回非零退出码。不要据此声称整门课已自动完成。

脚本的默认 URL 只是通用课程入口，默认课程名来自原始适配测试；处理自己的课程时务必显式传入 --url 和 --course-name。若页面显示 enc 校验失败，请重新取得当前有效 URL。若提示浏览器资料目录被占用，请先关闭上一次脚本打开的专用 Chrome。

## 本地输出与隐私

- output/lesson-inventory/：只读清点结果。
- output/complete-course/：逐节资源、视频分类、测验状态与检查点。
- output/quiz-completion/verified-answers.json：仅存页面 100 分且每题有答案依据的归档。
- output/course-summary/：逐节 JSON 和 CSV 汇总。

output/、.private/、本地测试及缓存均由 .gitignore 排除，不随仓库发布。提交前仍应检查 git status；.gitignore 不会自动移除已经被 Git 跟踪的文件，也不能替你识别复制到其他目录的敏感内容。
