# Beyond Watching and Searching: Benchmarking Agents for Video-Centered Deep Research Reports

**OmniDR-Bench** evaluates agents that turn a source video and a user request into an interactive research report supported by external evidence and traceable citations. It includes **50 bilingual tasks** and **6,414 binary rubric criteria** covering content, citation, and presentation.

[Paper](https://arxiv.org/abs/2610.xxxxx) · [Quick Start](#quick-start) · [Dataset](https://huggingface.co/datasets/marinero4972/OmniDR-Bench) · [Data Usage Notice](https://huggingface.co/datasets/marinero4972/OmniDR-Bench) · [Report Format](REPORT_FORMAT.md) · [Results](#results) · [Citation](#citation)

![OmniDR-Bench teaser](assets/teaser.png)

## Quick Start

### 1. Install

Requires **Python 3.10+**, **Node.js 18+**, and a user-configured OpenAI-compatible Chat Completions endpoint with text and vision judging support. Evaluation runs on CPU.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install --with-deps chromium
```

### 2. Prepare videos and reports

Obtain `OmniDR-Bench.jsonl` from [Hugging Face](https://huggingface.co/datasets/marinero4972/OmniDR-Bench). Store it in a directory of your choice, outside this code repository:

```text
OmniDR-Bench-data/
├── OmniDR-Bench.jsonl
└── videos/
    └── {id}.mp4              # supplied by the user
```

`OmniDR-Bench.jsonl` has **50 lines**, one task per line:

| Field | Contents |
|---|---|
| `id` | Unique task ID. |
| `video_url` | Public YouTube or Bilibili source-page URL. |
| `query` | User request in its original language. |
| `rubric` | Complete evaluation criteria and scoring configuration. |

See the [bilingual dataset usage notice](https://huggingface.co/datasets/marinero4972/OmniDR-Bench) before using the data.

**Videos and audio are not distributed.** Users must obtain the source videos independently in accordance with the applicable terms and permissions, and place their local copies at `videos/{id}.mp4` inside their data directory, for example `OmniDR-Bench-data/videos/OmniDR-Bench-v0-0001.mp4`. This repository provides no video download instructions or tools.

The reference harness for generating reports will be integrated into Qwen-MM-Plugins.

Give the video and query to the evaluated system; reserve the rubric for scoring. Place each complete generated report under its model and task ID:

```text
reports/
└── my_model/
    └── OmniDR-Bench-v0-0001/
        ├── index.html
        ├── js/data.js
        └── ...              # all referenced scripts, styles, images, and media
```

A `report/` wrapper inside the task folder is also accepted. Reports must include their referenced local resources with working relative paths; see [Report Format](REPORT_FORMAT.md). No model-generated reports or evaluation outputs are bundled.

### 3. Configure judges

```bash
cp .env.example .env
```

Set `OPENAI_BASE_URL` and `OPENAI_API_KEY` in `.env`. The base URL is the API prefix before `/chat/completions`, usually ending in `/v1`. The repository includes no endpoint or credentials.

`OMNIDR_JUDGE_MODEL` selects the judge (default: `gpt-5.6-sol`, as used in the paper). The same model handles text and vision judgments, and recency checks use today’s date. If needed, override these defaults with `--vision-model` or `--evaluation-date YYYY-MM-DD`. Environment variables override `.env`.

### 4. Evaluate

Run from the repository root:

```bash
# Validate report structure without API calls or browser rendering.
python evaluate.py --reports-dir ./reports --data-dir /path/to/OmniDR-Bench-data --validate-only

# Evaluate all submitted model folders on all 50 tasks.
python evaluate.py --reports-dir ./reports --data-dir /path/to/OmniDR-Bench-data

# Evaluate one model and one task.
python evaluate.py --reports-dir ./reports --data-dir /path/to/OmniDR-Bench-data \
  --models my_model --instances OmniDR-Bench-v0-0001
```

`--data-dir` is required and must point to the directory containing your `OmniDR-Bench.jsonl`. This code repository does not include a data directory. Separate query and rubric files are not required; the runner prepares per-task inputs in its results cache.

Use `--jobs` for concurrent reports and `--workers` for concurrent judgments within each report (defaults: 1 and 4). `--no-llm` runs machine and browser checks without judge API calls; `--skip-render` disables browser checks. Both modes produce **partial scores**, not full benchmark results. See `python evaluate.py --help` for all options.

Re-running resumes compatible cached judgments and retries unresolved ones. Changes to a task, report, judge settings, or evaluation date invalidate the corresponding cache. Use `--force` to re-evaluate.

Outputs are written to `results/` (override with `--output-dir`):

- `summary.json`: model statistics and per-task status.
- `scores.csv`: task totals and dimension scores.
- `{model}/{id}.json`: detailed rubric judgments and scores.
- `.cache/`: per-task inputs, browser measurements, and resume metadata.

The primary metric, `by_model[model].total_score.mean` in `summary.json`, averages **all selected tasks**, assigning zero to missing or unreadable reports. `judged_only` covers reports with scores. Check `evaluation_complete`, `n_errored`, and `n_unresolved` before reporting results. Exit codes: `0` for a completed run, `1` for an incomplete evaluation, and `2` for input or configuration errors. Partial modes remain labeled as partial even when the command succeeds.

## Scoring

The rubric hierarchy is **dimension → direction → binary criterion**, with 23 directions across three dimensions:

| Dimension | Weight |
|---|---:|
| Content | 0.50 |
| Citation | 0.30 |
| Presentation | 0.20 |

Criteria use programmatic checks, a text judge, or a screenshot-based vision judge. Gate criteria use three text-judge votes by default; a failed gate zeroes its direction. Core directions receive continuous credit without a direction-level pass/fail threshold.

Direction weights depend on their scored criterion counts. Fact coverage, causal analysis, and comparison cap this weight at 15 criteria and use `min(1, coverage / 0.18)` credit. The total is a weighted dimension mean on a 0–100 scale. Guarded N/A propagation handles related content and citation criteria; evaluation errors are tracked separately. See the [paper](https://arxiv.org/abs/2610.xxxxx) for the full protocol.

## Benchmark Construction & Statistics

![Audited Rubric Construction](assets/benchmark_construction.png)

![OmniDR-Bench statistics](assets/data_statistics.png)

The 50 tasks cover four task families and 19 subcategories, with 27 English and 23 Chinese queries. They reference 42 distinct source videos; some videos support multiple tasks with different requests.

## Results

![Main results from the paper](assets/results.png)

Paper Table 3 compares 18 model–harness configurations. **Avg.** covers all 50 tasks, assigning zero to unsuccessful reports. **Avg. succ.** and dimension columns cover scored reports; **Success** is their proportion. Quality scores use a 0–100 scale.

## License

Evaluation code: [MIT](LICENSE). Dataset texts and arrangement: [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/legalcode.en), as specified in the [full bilingual data usage notice](https://huggingface.co/datasets/marinero4972/OmniDR-Bench). Neither license extends to source videos, audio tracks, or other third-party materials.

## Citation

```bibtex
@article{meng2026omnidr,
  title={Beyond Watching and Searching: Benchmarking Agents for Video-Centered Deep Research Reports},
  author={Meng, Jiahao and Yang, Qize and Wang, Yuxuan and Chu, Yunfei and Dai, Wensi and Xu, Jin and Tong, Yunhai},
  journal={arXiv preprint arXiv:2610.xxxxx},
  year={2026}
}
```
