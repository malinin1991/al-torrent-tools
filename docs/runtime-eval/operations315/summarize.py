"""Render the completed independent core/supplement, retaining all raw samples."""
import hashlib,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[3]
DOCS=ROOT/'docs'
FILES=['operation_benchmark_core_linux_arm64.json','operation_benchmark_supplement_linux_arm64.json']
LABELS={
 ('hevc_http','cold_request'):'HEVC: первый HTTP request нового процесса',
 ('hevc_http','warm_request'):'HEVC: HTTP request после двух прогревочных',
 ('mediainfo_real_full','operation'):'MediaInfo full: 4 настоящих MKV',
 ('mediainfo_real_changed','operation'):'MediaInfo changed incremental: 4 настоящих MKV',
 ('mediainfo_generated_full','operation'):'MediaInfo full: 96 созданных MKV',
 ('mediainfo_generated_changed','operation'):'MediaInfo changed incremental: 96 созданных MKV',
 ('hash_real','operation'):'Whole hash handler: 4 настоящих MKV, qB inventory fixture',
}

def ci_text(metric):
 lo,hi=metric['paired_bootstrap_95pct_ci']
 return f"{metric['change_pct']:+.2f}% (95% CI {100*(lo-1):+.2f}…{100*(hi-1):+.2f}%)"

def load_all():
 result={};raw=[]
 for name in FILES:
  p=DOCS/name;data=json.loads(p.read_text())
  assert data['rounds']==12 and len(data['samples'])==216,(name,'incomplete')
  result.update(data['comparisons'])
  raw.append(dict(file=name,sha256=hashlib.sha256(p.read_bytes()).hexdigest(),samples=len(data['samples']),warmups=len(data['warmups'])))
 return result,raw

def main():
 comparisons,raw=load_all()
 report=['# Целые операции HEVC / MediaInfo: CPython 3.15 и 3.15t',
 'Итоговое решение: рабочий runtime — обычный CPython 3.15.0 с GIL; в рабочий код перенесён default incremental4 с сохранением explicit workers1. Выбор policy сделан по приоритету typical wall time: median real4 улучшился на27.67%, generated96 на46.53%. P95 nonregression НЕ доказан: real4 point384→454ms, generated96 774→815ms, широкие CI сохранены. HEVC pool и 3.15t остаются экспериментальными. Это явный tradeoff median latency, не объявление p95 pass.',
 'До/после сравниваются одинаковые реальные операции с PostgreSQL 16.15 и native MediaInfo 26.10 на Linux ARM64 в Docker Desktop. Основной критерий — wall latency. Core и supplement — независимые серии по 12 сбалансированных rounds, по 216 samples и 18 исключённых correctness/warmup samples. Все выбросы сохранены.',
 'HEVC оптимизация устраняет повторный full pairing; MediaInfo собирает результаты native parse в bounded batch до 25 и записывает их на caller Session. В core full scan сохраняет4 parse workers, incremental1 — промежуточная policy; окончательный incremental4 измерен отдельно; whole hash follow-up использует 4 вместо последовательного parse. Финальные5 production modules byte-identical проверенному workers candidate;1041 full tests прошли во всех4режимах (regular pure/t-on/t-off/regular binary).',
 'Regular dedup улучшил HEVC HTTP примерно на14–16%; batch ускорил generated96 MediaInfo на25–41%. На user4 отдельный full scan и whole hash не доказали устойчивого ускорения. HEVC persistent pool не окупился по whole HTTP против dedup regular; t-off compute improvement не заменяет end-to-end latency. Ordinary runtime выбран root; Unraid AMD64 и production binary backend performance здесь не измерены.',
 '## Измерения',
 'Ниже абсолютные median / p95 указаны в ms. Процент median — медиана парных отношений, поэтому может отличаться от отношения двух абсолютных медиан. CI: 10 000 paired bootstrap resamples, seed 315314. Для p95 пересчитываются квантили на парных bootstrap indices. Диагностический допуск отсутствия регрессии — 3%; CI, пересекающий предел, означает неопределённость, а не pass. CPU/RAM вторичны и не являются отказным gate.']
 derived=dict(raw=raw,diagnostic_noise_margin_pct=3,operations={})
 for (case,stage),label in LABELS.items():
  prefix=f'{case}/{stage}'
  regular=comparisons[prefix+'/optimized_vs_baseline/regular']
  ft=comparisons[prefix+'/optimized_ft_off_vs_optimized_regular']
  control=comparisons[prefix+'/optimized_ft_on_vs_optimized_regular']
  time=regular['elapsed_sec'];a,b=time['baseline'],time['candidate'];f=ft['elapsed_sec']['candidate']
  report += [f'### {label}',
   f"Regular: **{a['median']*1000:.2f} / {a['p95']*1000:.2f} → {b['median']*1000:.2f} / {b['p95']*1000:.2f} ms**. Парный median: {ci_text(time['median'])}; p95: {ci_text(time['p95'])}.",
   f"Оптимизированный t GIL-off: **{f['median']*1000:.2f} / {f['p95']*1000:.2f} ms**. Относительно optimized regular: median {ci_text(ft['elapsed_sec']['median'])}; p95 {ci_text(ft['elapsed_sec']['p95'])}."]
  cpu=regular['cpu_sec'];f_cpu=ft['cpu_sec'];rss=regular['linux_vm_hwm_bytes'];f_rss=ft['linux_vm_hwm_bytes']
  delta=f_rss['absolute_delta'];lo,hi=delta['paired_bootstrap_95pct_ci_mib']
  report.append(f"CPU time процесса, median before regular / after regular / after t-off: {cpu['baseline']['median']*1000:.2f} / {cpu['candidate']['median']*1000:.2f} / {f_cpu['candidate']['median']*1000:.2f} ms; t-off vs after regular {ci_text(f_cpu['median'])}. Whole-child VmHWM: {rss['baseline']['median']/1048576:.2f} / {rss['candidate']['median']/1048576:.2f} / {f_rss['candidate']['median']/1048576:.2f} MiB; парная t-off разница {delta['median_delta_mib']:+.2f} MiB (95% CI {lo:+.2f}…{hi:+.2f}).")
  derived['operations'][prefix]=dict(optimized_regular_vs_baseline_regular=regular,
      optimized_ft_off_vs_optimized_regular=ft,optimized_ft_on_vs_optimized_regular=control,
      optimized_ft_off_vs_baseline_regular=comparisons[prefix+'/optimized_ft_off_vs_baseline_regular'])
 extra_sections=[]
 extra_inputs=[('pool','operation_benchmark_hevc_pool_linux_arm64.json',72),('workers','operation_benchmark_mediainfo_workers_linux_arm64.json',144)]
 for name,filename,expected_samples in extra_inputs:
  path=DOCS/filename
  if not path.exists():continue
  extra=json.loads(path.read_text())
  assert extra['rounds']==12 and len(extra['samples'])==expected_samples,(filename,'incomplete')
  derived['raw'].append(dict(file=filename,sha256=hashlib.sha256(path.read_bytes()).hexdigest(),samples=len(extra['samples']),warmups=len(extra['warmups'])))
  derived[name]=extra['comparisons']
  if name=='pool':
   extra_sections += ['## Отдельный HEVC pool candidate: dedup-only → dedup+pool',
    'Самостоятельная серия12rounds/72samples плюс6 excluded warmups. Baseline здесь — optimized-final dedup-only, candidate — snapshot+lazy persistent4 pool. SQL/ORM/snapshot/serialization/compute/merge/Jinja входят actual request. Четыре contiguous release tasks заданы до измерений:1024→4×256. created_count0→1→1 подтверждён; regular/t-on cold3threads→warm4, t-off4→4. Ни production, ни optimized-final этим кандидатом не изменены.']
   prefixes=['hevc_http/cold_request','hevc_http/warm_request']
  else:
   extra_sections += ['## Отдельный финальный MediaInfo candidate: original → batch+incremental4',
    'Самостоятельная серия12rounds/144samples плюс12 excluded warmups. Baseline ORIGINAL sequential incremental1/per-file commits, candidate batch+default incremental4; explicit workers1 сохраняется. Params только mode=incremental, без workers override. Core batch+worker1 остаётся промежуточной отдельной серией; эффекты не складываются и cross-series paired CI не вычисляются. Все samples выполняют настоящий native parse4/96 и DB updates, peak active parse calls диагностируются без подмены parser.']
   prefixes=['mediainfo_real_changed/operation','mediainfo_generated_changed/operation']
  for prefix in prefixes:
   regular=extra['comparisons'][prefix+'/optimized_vs_baseline/regular']
   reference='/optimized_ft_off_vs_baseline_regular' if name=='pool' else '/optimized_ft_off_vs_optimized_regular'
   ft=extra['comparisons'][prefix+reference]
   own_ft=extra['comparisons'][prefix+'/optimized_vs_baseline/ft_off']
   t=regular['elapsed_sec'];a,b=t['baseline'],t['candidate'];f=ft['elapsed_sec']['candidate']
   label='первый request' if prefix.endswith('cold_request') else 'warm request' if prefix.endswith('warm_request') else 'real4 changed' if 'real_changed'in prefix else 'generated96 changed'
   extra_sections += [f'### {label}',
    f"Regular before→after median/p95: **{a['median']*1000:.2f}/{a['p95']*1000:.2f} → {b['median']*1000:.2f}/{b['p95']*1000:.2f} ms**. Парный median {ci_text(t['median'])}; p95 {ci_text(t['p95'])}.",
    f"T-off candidate median/p95: **{f['median']*1000:.2f}/{f['p95']*1000:.2f} ms**; относительно {'dedup-only regular' if name=='pool' else 'final regular candidate'}: median {ci_text(ft['elapsed_sec']['median'])}; p95 {ci_text(ft['elapsed_sec']['p95'])}."]
   rcpu=regular['cpu_sec'];rrss=regular['linux_vm_hwm_bytes'];rd=rrss['absolute_delta'];rlo,rhi=rd['paired_bootstrap_95pct_ci_mib']
   extra_sections.append(f"Regular CPU before→after median {rcpu['baseline']['median']*1000:.2f}→{rcpu['candidate']['median']*1000:.2f}ms, {ci_text(rcpu['median'])}; whole-child VmHWM {rrss['baseline']['median']/1048576:.2f}→{rrss['candidate']['median']/1048576:.2f}MiB, paired delta {rd['median_delta_mib']:+.2f}MiB (95% CI {rlo:+.2f}…{rhi:+.2f}).")
   cpu=ft['cpu_sec'];rss=ft['linux_vm_hwm_bytes'];delta=rss['absolute_delta'];lo,hi=delta['paired_bootstrap_95pct_ci_mib']
   extra_sections.append(f"T-off CPU median {cpu['candidate']['median']*1000:.2f} ms, изменение к тому же reference {ci_text(cpu['median'])}; whole-child VmHWM {rss['candidate']['median']/1048576:.2f} MiB, парная разница {delta['median_delta_mib']:+.2f} MiB (95% CI {lo:+.2f}…{hi:+.2f}).")
   if name=='pool':extra_sections.append(f"Внутри t-off dedup→pool: median {ci_text(own_ft['elapsed_sec']['median'])}; p95 {ci_text(own_ft['elapsed_sec']['p95'])}. Это сравнение самого t режима, не выигрыш перед обычным regular runtime.")
 report += extra_sections
 report += ['## Граница и ограничения',
 'HEVC timer — TestClient GET до полностью готового HTTP response, включая PostgreSQL SQL/ORM, grouping, pairing, sorting и Jinja. Transport ASGI in-process: TCP/TLS/browser исключены. Regex/assert/full HTML SHA выполняются после timer. HEVC UTC clock фиксирован только в harness на 2026-10-13T00:00:00; fixture dates без offset сохраняют fresh/missing/overdue/history/ignored сценарии. Cold означает первый request нового процесса; import/startup вне request timer, БД/диск не объявляются холодными. Production pool в HEVC отсутствует.',
 'MediaInfo и hash wall — целый direct job handler, включая реальные native file IO/parse, BLAKE3 (hash case), нормализацию, production cache-gate lookups, PostgreSQL writes/commits/logging/audit. JobRunner scheduling/claim/status transitions не входят. Setup/reset/stale gate preparation/correctness queries вне wall timer. В каждом real sample parsed=4; hash sample также hashed=4, gated=0, errors=0, sticky new и hash_progress/hash_done/ui_status PipelineEvents. Generated sample parsed=96, updated=96, errors=0. Смысловые payloads / metadata / hashes / audit и полный HTML совпадают между source/runtime комбинациями. HEVC synthetic fixture:1024releases/5248archives/43392files; реальные медиа/MediaInfo rows, активные jobs и production pipeline population в этой странице не воспроизводились, поэтому это end-to-end application fixture, не нагрузочная копия пользовательской БД.',
 'Четыре user MKV (1 500 398 629 bytes) mounted readonly, originals не изменялись. Host file cache не сбрасывался: warm cache, не физические холодные диски Unraid. Changed incremental предварительно получает настоящие reports и изменяет только их тестовый DB mtime на -60. Generated 96 × 117 980 bytes — копии собственного законного FFmpeg AVC/AAC 3s clip, отдельный batch workload; это не разнообразный real-media corpus. Whole hash подменяет только внешнюю qB inventory через FileTracker._qb_paths_and_priorities; реальные hashing/parse/DB/audit сохранены, qB HTTP latency не измерена.',
 '46 identical distributions, pure psycopg3.3.6 + libpq170011 во всех режимах; это честная нормализация эксперимента, а не parity с production psycopg-binary. Native imports и фактический GIL True/True/False проверены, forced -Xgil=0 отсутствует. Core1036 tests, HEVC pool1106tests, окончательный workers candidate1041tests: каждый набор прошёл во всех своих normalized modes; core/workers также проверены на regular binary candidate. PostgreSQL fsync/synchronous_commit/full_page_writes ON. CPU quota=max, affinity/cpuset 0–9, Docker VM10 CPUs. Нативный Unraid AMD64 здесь не измерялся.',
 'CPU/RSS относятся только к application child, не PostgreSQL. ru_maxrss/VmHWM — high-water mark всего child, включая imports/reset и native prepopulation changed-case до timer; rss_before_work также сохранён. Это preparation-inclusive диагностический RAM signal, не изолированный peak конкретной job. Fork/exec HWM floor возможен; /proc VmHWM сохранён отдельно.',
 'Пилот остановлен по методическому решению root: HTTP validation ещё была внутри timer. Все его 42 завершённых samples сохранены отдельно и никогда не объединены с итогом. Окончательный exact runner и протокол v2 находятся в evidence.',
 '## Воспроизведение',
 'Все4 source manifests и clean archives, exact runner, dataset manifest, runtime/image/native/CPU provenance, PostgreSQL settings и полные validation logs находятся в [runtime-eval/operations315](runtime-eval/operations315/). Runtime build inputs сохранены в [runtime-eval/315t](runtime-eval/315t/). Скрипты run.sh/run_supplement.sh фиксируют readonly mounts и image tag; сверять сохранённый image ID, поскольку tag сам по себе изменяем.',
 'Создать isolated internal network/PG без host ports, восстановить clean source snapshots в пути run.sh, подготовить собственные fixtures generate_fixtures.py; реальные MKV только readonly. Затем:',
 '```sh\n./docs/runtime-eval/operations315/run.sh --prepare --source /bench/baseline\n./docs/runtime-eval/operations315/run.sh --baseline /bench/baseline --optimized /bench/optimized --rounds 12 --cases hevc_http mediainfo_real_full mediainfo_real_changed --output /bench/results/operation_benchmark_core_v2_linux_arm64.json\n./docs/runtime-eval/operations315/run_supplement.sh --baseline /bench/baseline --optimized /bench/optimized --output /bench/results/operation_benchmark_supplement_linux_arm64.json\npython3 docs/runtime-eval/operations315/summarize.py\n```',
 'Перед summarize.py скопировать четыре completed raw JSON в docs/operation_benchmark_core_linux_arm64.json , docs/operation_benchmark_supplement_linux_arm64.json, docs/operation_benchmark_hevc_pool_linux_arm64.json и docs/operation_benchmark_mediainfo_workers_linux_arm64.json. Повторять на свободном runtime без параллельных suites/builds. Не удалять пользовательские файлы или unrelated Docker resources.',
 'Raw: [core](operation_benchmark_core_linux_arm64.json), [supplement](operation_benchmark_supplement_linux_arm64.json), [пилот](operation_benchmark_core_pilot_boundary_linux_arm64.json), [derived comparisons](operation_benchmark_summary_linux_arm64.json), [pool](operation_benchmark_hevc_pool_linux_arm64.json), [incremental4 candidate](operation_benchmark_mediainfo_workers_linux_arm64.json).'
 ]
 (DOCS/'OPERATION_BENCHMARKS_315.md').write_text('\n\n'.join(report)+'\n')
 (DOCS/'operation_benchmark_summary_linux_arm64.json').write_text(json.dumps(derived,indent=2)+'\n')

if __name__=='__main__':main()
