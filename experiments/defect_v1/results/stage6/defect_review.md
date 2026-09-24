# Defect candidate review

- review id: `provenlattice-defect-v1-review`
- status: `DEFECT_REVIEW_READY_FOR_ANNOTATION` / claims `NO_PRECISION_VERDICT_YET`
- seed: `provenlattice-stage6-review`
- code: `0c27e011f91a324b327751fb5859193f64b4a719` (dirty working tree)
- subject join: `identity`
- contracts: `on`
- sampling: repository x defect_key x resolution_status, round-robin, one sample across all repositories, target 32 (selected 32)

Verdicts are recorded in the JSON file's `annotation` block:

| field | what it is |
|---|---|
| `verdict` | the review *state*: `PENDING` → `REVIEWED` |
| `is_true_positive` | the judgment: `true` / `false`, or `null` with `verdict: REVIEWED` for **uncertain** — you looked and the sheet did not carry what the decision needed. Counted separately, not as pending |
| `failure_reason` | why it is a false positive, from the closed list (`identity` / `extractor` / `missing_read_write` / `alias` / `ownership` / `contract` / `call_argument_loss` / `insufficient_context` / `other`) |
| `needs_dfg` | is a **data-flow** edge the piece that is missing — the value's propagation, or two names that may be one object? If what is missing is identity, an event, ownership or an API contract, this is `false` and `missing_capability` says which |
| `missing_capability` | what was missing, in words |
| `reason` / `confidence` / `annotator` / `notes` | free text, optional |

`needs_dfg` and `missing_capability` are what answer stage 4's acceptance question ③, and they are two different questions: a false positive caused by a name that is not the object is not fixed by a data-flow edge, and counting it as one would be the mistake this stage exists to avoid.

## Sources

| repository | database | generation | reviewed code | candidates | limits | root |
|---|---|---|---|---|---|---|
| llama.cpp-69 | `d:\tmp\stage5b\llama69-5b.db` | 1 | `3cf03257f219 (dirty)` | 134984 | double_free=ok, error_handling=ok, lock_order=ok, null_flow=ok, race_condition=ok, resource_lifetime=recall, taint_path=ok, use_after_free=ok | yes |
| redis-50 | `d:\tmp\stage5b\redis50-5b.db` | 1 | `5f08991bce47 (dirty)` | 19196 | double_free=ok, error_handling=ok, lock_order=ok, null_flow=ok, race_condition=ok, resource_lifetime=recall, taint_path=ok, use_after_free=ok | yes |

## Availability

| repository | key | status | population_N | sample_n | population capped | recall truncated |
|---|---|---|---|---|---|---|
| llama.cpp-69 | 1.1 | ambiguous | 101159 | 2 | no | no |
| llama.cpp-69 | 1.4 | ambiguous | 13 | 2 | no | no |
| llama.cpp-69 | 1.4 | resolved | 5 | 2 | no | no |
| llama.cpp-69 | 3.1 | ambiguous | 94 | 1 | no | no |
| llama.cpp-69 | 3.1 | resolved | 497 | 1 | no | no |
| llama.cpp-69 | 4.1 | ambiguous | 351 | 1 | no | yes |
| llama.cpp-69 | 4.1 | resolved | 47 | 1 | no | yes |
| llama.cpp-69 | 4.1 | unresolved | 9 | 1 | no | yes |
| llama.cpp-69 | 4.2 | resolved | 3 | 1 | no | no |
| llama.cpp-69 | 4.2 | unresolved | 280 | 1 | no | no |
| llama.cpp-69 | 4.3 | ambiguous | 30 | 1 | no | no |
| llama.cpp-69 | 4.3 | resolved | 1 | 1 | no | no |
| llama.cpp-69 | 4.6 | ambiguous | 27 | 1 | no | yes |
| llama.cpp-69 | 4.6 | resolved | 2 | 1 | no | yes |
| llama.cpp-69 | 9.1 | ambiguous | 5016 | 1 | no | no |
| llama.cpp-69 | 9.1 | resolved | 27450 | 1 | no | no |
| redis-50 | 1.1 | ambiguous | 14439 | 2 | no | no |
| redis-50 | 1.4 | ambiguous | 1 | 1 | no | no |
| redis-50 | 3.1 | ambiguous | 51 | 1 | no | no |
| redis-50 | 3.1 | resolved | 11 | 1 | no | no |
| redis-50 | 4.1 | ambiguous | 5 | 1 | no | yes |
| redis-50 | 4.1 | resolved | 9 | 1 | no | yes |
| redis-50 | 4.2 | resolved | 194 | 1 | no | no |
| redis-50 | 4.3 | ambiguous | 387 | 1 | no | no |
| redis-50 | 4.6 | ambiguous | 12 | 1 | no | yes |
| redis-50 | 4.6 | resolved | 3 | 1 | no | yes |
| redis-50 | 9.1 | ambiguous | 680 | 1 | no | no |
| redis-50 | 9.1 | resolved | 3404 | 1 | no | no |

## Cases

### DFV1-llama.cpp-69-f1859bef6ff47b31

- type: `1.1` / `race_condition`
- status: `ambiguous` (confidence 0.5)
- subject: `output` in `llama_model_deepseek.load_arch_tensors`
- candidate: `E-DEFECT-f4603853aa663c92cf34b8c5`
- anchor: `event:02025a719638d748d3fa2f45891a2b265cb0abcb4f692e63041e6228cbe5d72b`
- source: `src/models/deepseek.cpp:29-29`, `src/models/deepseek.cpp:31-31`, `src/models/deepseek.cpp:30-30`, `src/models/baichuan.cpp:23-23`

Facts:

- `event`='WRITE', `matched_name`='output', `method`='llama_model_deepseek.load_arch_tensors', `point`='event:02025a719638d748d3fa2f45891a2b265cb0abcb4f692e63041e6228cbe5d72b', `source`='src/models/deepseek.cpp:29-29', `subject`='output', `subject_from`=''
- `event`='WRITE', `matched_name`='output', `method`='llama_model_deepseek.load_arch_tensors', `point`='event:0a680fb48a0da50f8adb0ef18281bc6a1749a3464ae448cd098f86e6804a4c35', `source`='src/models/deepseek.cpp:31-31', `subject`='output', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='llama_model_deepseek.load_arch_tensors', `point`='event:16598f4b551fed51c5ebcff74f38a54895392e83f1ece70f375ed8f24979fe6d', `source`='src/models/deepseek.cpp:31-31', `subject`='output', `subject_from`='assignment'
- `event`='CHECK', `matched_name`='!output', `method`='llama_model_deepseek.load_arch_tensors', `point`='event:17183fcb3f184df0a4ab959611dd8bd4044ca7390b18c3e0ab63a47930d6631e', `source`='src/models/deepseek.cpp:30-30', `subject`='output', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='llama_model_deepseek.load_arch_tensors', `point`='event:2c01e4e9cd21e938eebc24868c6165c6464547444f7bd65dcdc1820805605450', `source`='src/models/deepseek.cpp:29-29', `subject`='output', `subject_from`='assignment'
- `event`='CALL', `matched_name`='call_expression', `method`='llama_model_baichuan.load_arch_tensors', `point`='event:5a87ecda60db50bf33d9271c8268ed29ea53ffb7462a98de7770a0eab49505ae', `source`='src/models/baichuan.cpp:23-23', `subject`='output', `subject_from`='assignment'
- `event`='CALL', `matched_name`='call_expression', `method`='llama_model_deepseek.load_arch_tensors', `point`='event:6d4b757ccf31dfbef85cfac031f5bd0d2bf95c597add95072c2253c895a62c59', `source`='src/models/deepseek.cpp:29-29', `subject`='output', `subject_from`='assignment'
- `event`='CALL', `matched_name`='call_expression', `method`='llama_model_baichuan.load_arch_tensors', `point`='event:96f9b56d74803032777869e03498c15d7d74ab20180982909bb18e94eaf38182', `source`='src/models/baichuan.cpp:23-23', `subject`='output', `subject_from`='assignment'
- `event`='CALL', `matched_name`='call_expression', `method`='llama_model_deepseek.load_arch_tensors', `point`='event:aa06ed8d188463d685cd6138009faa7a758248e9276542c3072a5aac1504eea8', `source`='src/models/deepseek.cpp:31-31', `subject`='output', `subject_from`='assignment'
- `event`='WRITE', `matched_name`='output', `method`='llama_model_baichuan.load_arch_tensors', `point`='event:b96558b6fe24f0fdc6af031378b6caad1b381326792928c5d6b94b6976380dca', `source`='src/models/baichuan.cpp:23-23', `subject`='output', `subject_from`=''
- `event`='READ', `matched_name`='output', `method`='llama_model_deepseek.load_arch_tensors', `point`='event:bb731a9036e0b9146bd91b7e784b813a7dec1bb4c4630e821ad80d2194c5a79f', `source`='src/models/deepseek.cpp:30-30', `subject`='output', `subject_from`=''

Uncertain facts:

- `decisive`=True, `detail`='the repository starts threads somewhere, so neither method is ordered', `fact`='MAY_PARALLEL', `status`='ambiguous'
- `decisive`=True, `detail`='no lock in this method', `fact`='PROTECTED_BY', `lock`='', `method`='llama_model_baichuan.load_arch_tensors', `status`='ambiguous'
- `decisive`=True, `detail`='no lock in this method', `fact`='PROTECTED_BY', `lock`='', `method`='llama_model_deepseek.load_arch_tensors', `status`='ambiguous'
- `decisive`=True, `detail`='access kind from READ/WRITE events: write vs write', `fact`='ACCESS_KIND', `status`='ambiguous'

Missing evidence:

- whether the two contexts really run concurrently: MAY_PARALLEL is undecidable from spawn points alone
- whether the lock actually guards this variable: holding a lock near an access is not the same as the lock protecting the data
- whether the variable tolerates weak consistency (a statistics counter may be allowed to race by design)

```cpp
    output_norm = create_tensor(tn(LLM_TENSOR_OUTPUT_NORM, "weight"), {n_embd}, 0);
    // try to load output.weight, if not found, use token_embd (tied embeddings)
    output      = create_tensor(tn(LLM_TENSOR_OUTPUT,      "weight"), {n_embd, n_vocab}, TENSOR_NOT_REQUIRED);
    if (!output) {
        output = create_tensor(tn(LLM_TENSOR_TOKEN_EMBD, "weight"), {n_embd, n_vocab}, TENSOR_DUPLICATED);
```

```cpp
    output      = create_tensor(tn(LLM_TENSOR_OUTPUT,      "weight"), {n_embd, n_vocab}, TENSOR_NOT_REQUIRED);
    if (!output) {
        output = create_tensor(tn(LLM_TENSOR_TOKEN_EMBD, "weight"), {n_embd, n_vocab}, TENSOR_DUPLICATED);
    }

```

```cpp
    // try to load output.weight, if not found, use token_embd (tied embeddings)
    output      = create_tensor(tn(LLM_TENSOR_OUTPUT,      "weight"), {n_embd, n_vocab}, TENSOR_NOT_REQUIRED);
    if (!output) {
        output = create_tensor(tn(LLM_TENSOR_TOKEN_EMBD, "weight"), {n_embd, n_vocab}, TENSOR_DUPLICATED);
    }
```

```cpp
    {
        output_norm = create_tensor(tn(LLM_TENSOR_OUTPUT_NORM, "weight"), {n_embd}, 0);
        output      = create_tensor(tn(LLM_TENSOR_OUTPUT,      "weight"), {n_embd, n_vocab}, 0);
    }

```

Verdict (recorded in `defect_review.json`):

- `verdict`: `REVIEWED`
- `is_true_positive`: `false` — **false positive**
- `failure_reason`: `identity`
- `needs_dfg`: `false`
- `missing_capability`: `声明身份与作用域（declaration identity / scope）`
- `confidence`: `0.85`
- `reason`: `` 主体身份 `name:output` 把 src/models/deepseek.cpp:29 的写点（llama_model_deepseek::load_arch_tensors 的 output 成员）与 src/models/baichuan.cpp 侧的同名成员连成同一对象——两个不同模型类各自的成员变量，仅拼写相同；且两侧都是模型加载期（单线程）的初始化，不存在并发写读对。 ``
- `annotator`: `AI 提议 + 人工确认（Claude Code 提议 / Kyber5323 逐条确认，2026-09-24）`
- `notes`: `null`

### DFV1-redis-50-bdf42a3680959f46

- type: `1.1` / `race_condition`
- status: `ambiguous` (confidence 0.5)
- subject: `server` in `updateCachedTimeWithUs`
- candidate: `E-DEFECT-ca8975049d00102771927088`
- anchor: `event:0a6b2ad71803678f04adab5cf3d456b35132023b7b790ed03d9f3f08c746f978`
- source: `src/server.c:1382-1382`, `src/server.c:2420-2420`, `src/server.c:1383-1383`

Facts:

- `event`='WRITE', `matched_name`='server.mstime', `method`='updateCachedTimeWithUs', `point`='event:0a6b2ad71803678f04adab5cf3d456b35132023b7b790ed03d9f3f08c746f978', `source`='src/server.c:1382-1382', `subject`='server', `subject_from`=''
- `event`='READ', `matched_name`='server.mstime', `method`='initServerConfig', `point`='event:7c83cd4e8f86310b16149b8be1a94de9d074481da9f519f8adb8a62c200a3939', `source`='src/server.c:2420-2420', `subject`='server', `subject_from`=''
- `event`='READ', `matched_name`='server.mstime', `method`='updateCachedTimeWithUs', `point`='event:b742233de237ac07f20f6483f5b671270d249467fd6c74fe5ae8ea600d729b6b', `source`='src/server.c:1383-1383', `subject`='server', `subject_from`=''

Uncertain facts:

- `decisive`=True, `detail`='the repository starts threads somewhere, so neither method is ordered', `fact`='MAY_PARALLEL', `status`='ambiguous'
- `decisive`=True, `detail`='no lock in this method', `fact`='PROTECTED_BY', `lock`='', `method`='updateCachedTimeWithUs', `status`='ambiguous'
- `decisive`=True, `detail`='no lock in this method', `fact`='PROTECTED_BY', `lock`='', `method`='initServerConfig', `status`='ambiguous'
- `decisive`=True, `detail`='access kind from READ/WRITE events: write vs read', `fact`='ACCESS_KIND', `status`='ambiguous'

Missing evidence:

- whether the two contexts really run concurrently: MAY_PARALLEL is undecidable from spawn points alone
- whether the lock actually guards this variable: holding a lock near an access is not the same as the lock protecting the data
- whether the variable tolerates weak consistency (a statistics counter may be allowed to race by design)

```c
static inline void updateCachedTimeWithUs(int update_daylight_info, const long long ustime) {
    server.ustime = ustime;
    server.mstime = server.ustime / 1000;
    time_t unixtime = server.mstime / 1000;
    atomicSet(server.unixtime, unixtime);
```

```c
    initConfigValues();
    updateCachedTime(1);
    server.cmd_time_snapshot = server.mstime;
    getRandomHexChars(server.runid,CONFIG_RUN_ID_SIZE);
    server.runid[CONFIG_RUN_ID_SIZE] = '\0';
```

```c
    server.ustime = ustime;
    server.mstime = server.ustime / 1000;
    time_t unixtime = server.mstime / 1000;
    atomicSet(server.unixtime, unixtime);

```

Verdict (recorded in `defect_review.json`):

- `verdict`: `REVIEWED`
- `is_true_positive`: `false` — **false positive**
- `failure_reason`: `other`
- `needs_dfg`: `false`
- `missing_capability`: `执行阶段与线程模型（execution phase / thread model）`
- `confidence`: `0.8`
- `reason`: `写点 src/server.c:1383（updateCachedTimeWithUs）与读点 initServerConfig 无法并发：initServerConfig 是启动期一次性执行、先于任何线程创建；server.mstime 的周期更新也由主线程 cron 驱动。两个上下文在执行阶段上不重叠。图缺「初始化期 vs 运行期」的执行模型事实，MAY_PARALLEL 歧义在图内无法排除。`
- `annotator`: `AI 提议 + 人工确认（Claude Code 提议 / Kyber5323 逐条确认，2026-09-24）`
- `notes`: `null`

### DFV1-llama.cpp-69-fb4a226c969e08ff

- type: `1.4` / `lock_order`
- status: `ambiguous` (confidence 0.3)
- subject: `mutex_tasks` in `server_queue.worker_loop`
- candidate: `E-DEFECT-b292f22d9f0049c29f3d1cee`
- anchor: `event:6c87a86d2f2773c3c681af4f9f289d5f3adb15b2cc89842e6300e95405a5f4d4`
- source: `tools/server/server-queue.cpp:166-166`, `tools/server/server-queue.cpp:183-183`

Facts:

- `holding_confirmed`=False, `lock`='mutex_tasks', `method`='server_queue.worker_loop', `method_symbol_id`='symbol:8c403972984da0b810d8429cde41f4c514ffcacc33d74ef407503da33440f558', `point`='event:6c87a86d2f2773c3c681af4f9f289d5f3adb15b2cc89842e6300e95405a5f4d4', `role`='first', `side`='single', `source`='tools/server/server-queue.cpp:166-166'
- `holding_confirmed`=False, `lock`='mutex_tasks', `method`='server_queue.worker_loop', `method_symbol_id`='symbol:8c403972984da0b810d8429cde41f4c514ffcacc33d74ef407503da33440f558', `point`='event:fb96b90275ff43e65defc676f63a2a43e8c77bac8d6aa19807269d7f88135a71', `role`='second', `side`='single', `source`='tools/server/server-queue.cpp:183-183'

Uncertain facts:

- `decisive`=False, `detail`='a recursive mutex (PTHREAD_MUTEX_RECURSIVE) makes a second acquisition by the same thread legal', `fact`='MUTEX_RECURSIVE', `status`='ambiguous'
- `decisive`=True, `detail`='no path reaches the second acquisition without releasing the lock', `fact`='HOLDING_CONFIRMED', `status`='ambiguous'
- `decisive`=True, `detail`='the lock was recovered from a receiver or a declaration and its declared type is not a known mutex, so the name identifies an expression rather than the object it names', `fact`='LOCK_IDENTITY', `status`='ambiguous'
- `decisive`=True, `detail`='no declaration of this name in this file, so the identity falls back to the spelling -- a macro, an enum constant, or a global declared in a header the parser does not read', `fact`='IDENTITY_UNKNOWN', `point`='event:6c87a86d2f2773c3c681af4f9f289d5f3adb15b2cc89842e6300e95405a5f4d4', `source`='tools/server/server-queue.cpp:166-166', `status`='ambiguous'

Missing evidence:

- whether the mutex is recursive: under PTHREAD_MUTEX_RECURSIVE a second acquisition by the same thread is legal, and the graph does not hold the mutex's type
- the execution context: whether the two methods can run concurrently at all -- nothing in the graph orders them
- lock aliasing: two expressions naming the same mutex (`&s->m` and `s->m`) are two names here, and one name is one lock
- the failure path of a non-blocking acquisition: a try-lock that fails leaves the lock unheld and the graph cannot tell the two arms apart

```cpp
    while (true) {
        {
            std::unique_lock<std::mutex> lock(mutex_tasks);
            // wait on busy instead of yielding - busy stays set even when the yield already ended
            worker.cv.wait(lock, [&]{
```

```cpp
                terminated = process_new_tasks(true);
            } catch (...) {
                std::unique_lock<std::mutex> lock(mutex_tasks);
                worker.exception = std::current_exception();
                break;
```

Verdict (recorded in `defect_review.json`):

- `verdict`: `REVIEWED`
- `is_true_positive`: `false` — **false positive**
- `failure_reason`: `ownership`
- `needs_dfg`: `false`
- `missing_capability`: `RAII 守卫的析构释放（destructor release）`
- `confidence`: `0.85`
- `reason`: `` tools/server/server-queue.cpp:166 的 `std::unique_lock lock(mutex_tasks)` 是 cv.wait 用的内层块作用域守卫，块结束即析构释放；:183 catch 内的第二次获取是顺序获取，不嵌套。释放发生在 unique_lock 析构（无 UNLOCK 事件），图看不见。 ``
- `annotator`: `AI 提议 + 人工确认（Claude Code 提议 / Kyber5323 逐条确认，2026-09-24）`
- `notes`: `null`

### DFV1-redis-50-ef252e5f13617291

- type: `1.4` / `lock_order`
- status: `ambiguous` (confidence 0.3)
- subject: `index` in `hnsw_acquire_read_slot`
- candidate: `E-DEFECT-9db1a10827861139e8eaca56`
- anchor: `event:18e2419f8f5f3cb56021c99442e9b1ac630cfc007f6d6bdf25af96942346f48c`
- source: `modules/vector-sets/hnsw.c:2017-2017`, `modules/vector-sets/hnsw.c:2032-2032`

Facts:

- `holding_confirmed`=False, `lock`='index', `method`='hnsw_acquire_read_slot', `method_symbol_id`='symbol:406df0db7bc3af3e4c958ab16ab6d05c3cbf34ba272f0b42ef1f918064f074c6', `point`='event:18e2419f8f5f3cb56021c99442e9b1ac630cfc007f6d6bdf25af96942346f48c', `role`='first', `side`='single', `source`='modules/vector-sets/hnsw.c:2017-2017'
- `holding_confirmed`=False, `lock`='index', `method`='hnsw_acquire_read_slot', `method_symbol_id`='symbol:406df0db7bc3af3e4c958ab16ab6d05c3cbf34ba272f0b42ef1f918064f074c6', `point`='event:b16c7356168c168c4139c816ed13c0875f5d73cfe433921cf0e132ee0beeb43a', `role`='second', `side`='single', `source`='modules/vector-sets/hnsw.c:2032-2032'

Uncertain facts:

- `decisive`=False, `detail`='a recursive mutex (PTHREAD_MUTEX_RECURSIVE) makes a second acquisition by the same thread legal', `fact`='MUTEX_RECURSIVE', `status`='ambiguous'
- `decisive`=True, `detail`='no path reaches the second acquisition without releasing the lock', `fact`='HOLDING_CONFIRMED', `status`='ambiguous'
- `decisive`=True, `detail`='the name is the root of a longer expression (`s` in `s->buf`), so the same name elsewhere may be a different object', `fact`='SUBJECT_PATH', `point`='event:18e2419f8f5f3cb56021c99442e9b1ac630cfc007f6d6bdf25af96942346f48c', `source`='modules/vector-sets/hnsw.c:2017-2017', `status`='ambiguous'

Missing evidence:

- whether the mutex is recursive: under PTHREAD_MUTEX_RECURSIVE a second acquisition by the same thread is legal, and the graph does not hold the mutex's type
- the execution context: whether the two methods can run concurrently at all -- nothing in the graph orders them
- lock aliasing: two expressions naming the same mutex (`&s->m` and `s->m`) are two names here, and one name is one lock
- the failure path of a non-blocking acquisition: a try-lock that fails leaves the lock unheld and the graph cannot tell the two arms apart

```c
    for (uint32_t i = 0; i < HNSW_MAX_THREADS; i++) {
        if (pthread_mutex_trylock(&index->slot_locks[i]) == 0) {
            if (pthread_rwlock_rdlock(&index->global_lock) != 0) {
                pthread_mutex_unlock(&index->slot_locks[i]);
                return -1;
```

```c

    /* Get read lock. */
    if (pthread_rwlock_rdlock(&index->global_lock) != 0) {
        pthread_mutex_unlock(&index->slot_locks[slot]);
        return -1;
```

Verdict (recorded in `defect_review.json`):

- `verdict`: `REVIEWED`
- `is_true_positive`: `false` — **false positive**
- `failure_reason`: `other`
- `needs_dfg`: `false`
- `missing_capability`: `候选规则消费 holding 判定（candidate rule × holding verdict）`
- `confidence`: `0.8`
- `reason`: `modules/vector-sets/hnsw.c:2017 与 :2032 是同一函数内两个顺序的 rdlock 站点（循环内成功路径持锁返回；:2032 是循环后 fallback），不存在两把同锁都持有的路径——单子自己的 HOLDING_CONFIRMED 判定就写着 no path reaches the second acquisition without releasing the lock。图已判 holding 不成立仍产出候选，是「同方法双获取」候选规则的过度包含。`
- `annotator`: `AI 提议 + 人工确认（Claude Code 提议 / Kyber5323 逐条确认，2026-09-24）`
- `notes`: `null`

### DFV1-llama.cpp-69-4175f77da71d70dc

- type: `1.4` / `lock_order`
- status: `resolved` (confidence 0.6)
- subject: `mutex` in `llama_tensor_quantize_impl`
- candidate: `E-DEFECT-9c24082451618d6a10ffe7e8`
- anchor: `event:8c02bfa461be37acacaa7cc9595a7256efc11a8287ea44d588fd95e6ce88c92e`
- source: `src/llama-quant.cpp:780-780`, `src/llama-quant.cpp:800-800`

Facts:

- `holding_confirmed`=True, `lock`='mutex', `method`='llama_tensor_quantize_impl', `method_symbol_id`='symbol:a576148178b8c814b216d4ca9da46f5a88a3cde05ec00d187b4f8c64dbd09d8c', `point`='event:8c02bfa461be37acacaa7cc9595a7256efc11a8287ea44d588fd95e6ce88c92e', `role`='first', `side`='single', `source`='src/llama-quant.cpp:780-780'
- `holding_confirmed`=True, `lock`='mutex', `method`='llama_tensor_quantize_impl', `method_symbol_id`='symbol:a576148178b8c814b216d4ca9da46f5a88a3cde05ec00d187b4f8c64dbd09d8c', `point`='event:291314ad12412bebf354b6d4ae8d80230a4d255aef4e71af94d827a48869e749', `role`='second', `side`='single', `source`='src/llama-quant.cpp:800-800'

Uncertain facts:

- `decisive`=False, `detail`='a recursive mutex (PTHREAD_MUTEX_RECURSIVE) makes a second acquisition by the same thread legal', `fact`='MUTEX_RECURSIVE', `status`='ambiguous'

Missing evidence:

- whether the mutex is recursive: under PTHREAD_MUTEX_RECURSIVE a second acquisition by the same thread is legal, and the graph does not hold the mutex's type
- the execution context: whether the two methods can run concurrently at all -- nothing in the graph orders them
- lock aliasing: two expressions naming the same mutex (`&s->m` and `s->m`) are two names here, and one name is one lock
- the failure path of a non-blocking acquisition: a try-lock that fails leaves the lock unheld and the graph cannot tell the two arms apart

```cpp
        size_t local_size = 0;
        while (true) {
            std::unique_lock<std::mutex> lock(mutex);
            if (counter >= nrows) {
                if (local_size > 0) {
```

```cpp
            // validate the quantized data
            if (!ggml_validate_row_data(new_type, this_data, this_size)) {
                std::unique_lock<std::mutex> lock(mutex);
                valid = false;
                break;
```

Verdict (recorded in `defect_review.json`):

- `verdict`: `REVIEWED`
- `is_true_positive`: `false` — **false positive**
- `failure_reason`: `extractor`
- `needs_dfg`: `false`
- `missing_capability`: `守卫变量成员 .unlock() 的释放识别（guard-member unlock recognition）`
- `confidence`: `0.9`
- `reason`: `` src/llama-quant.cpp:780 的 `std::unique_lock lock(mutex)` 在 :792 有显式 `lock.unlock()`（取完行块即放锁再去量化），:800 的再次获取发生在验证失败路径、此刻锁已释放——不存在同线程自死锁。holding_confirmed=true 未把 :792 对守卫变量的成员 `.unlock()` 识别为释放事件。已读源码核对 :780-:803。 ``
- `annotator`: `AI 提议 + 人工确认（Claude Code 提议 / Kyber5323 逐条确认，2026-09-24）`
- `notes`: `源码核对：llama-quant.cpp:792 lock.unlock() 在 :800 再获取之前。`

### DFV1-redis-50-7c81a7e06079fa9c

- type: `3.1` / `null_flow`
- status: `ambiguous` (confidence 0.3)
- subject: `config` in `main`
- candidate: `E-DEFECT-5fa1b0278990a7bfeacda95e`
- anchor: `event:c9e6fb3861782cb045d7f7807a46a22134b991e726a219f8b7946b9964811385`
- source: `src/redis-benchmark.c:1760-1760`, `src/redis-benchmark.c:1803-1803`

Facts:

- `event`='WRITE', `matched_name`='config.cluster_nodes', `method`='main', `point`='event:dfde4031e37f8e7f85fec311b188bfa5c310ef2ba21261559ab210f91ceabf19', `source`='src/redis-benchmark.c:1760-1760', `subject`='config', `subject_from`=''
- `event`='DEREFERENCE', `matched_name`='config.cluster_nodes[i]', `method`='main', `point`='event:c9e6fb3861782cb045d7f7807a46a22134b991e726a219f8b7946b9964811385', `source`='src/redis-benchmark.c:1803-1803', `subject`='config', `subject_from`=''

Uncertain facts:

- `decisive`=True, `detail`='the name is the root of a longer expression (`s` in `s->buf`), so the same name elsewhere may be a different object', `fact`='SUBJECT_PATH', `point`='event:c9e6fb3861782cb045d7f7807a46a22134b991e726a219f8b7946b9964811385', `source`='src/redis-benchmark.c:1803-1803', `status`='ambiguous'
- `decisive`=True, `detail`='the definition has no position in the control-flow graph (a write is not an operation the walker steps through), so the graph cannot say the dereference runs after it', `fact`='REACH_UNKNOWN', `point`='event:dfde4031e37f8e7f85fec311b188bfa5c310ef2ba21261559ab210f91ceabf19', `source`='src/redis-benchmark.c:1760-1760', `status`='ambiguous'

Missing evidence:

- which calls can return NULL: the graph sees the allocation and the dereference, and whether this call's failure path is reachable at all is an API contract the AI side holds
- whether a guard covers every route to the dereference: the graph can say the null arm still reaches it, not whether some check upstream already excluded null on the paths that matter

```c
    config.cluster_mode = 0;
    config.cluster_node_count = 0;
    config.cluster_nodes = NULL;
    config.redis_config = NULL;
    config.is_fetching_slots = 0;
```

```c
        int i = 0;
        for (; i < config.cluster_node_count; i++) {
            clusterNode *node = config.cluster_nodes[i];
            if (!node) {
                fprintf(stderr, "Invalid cluster node #%d\n", i);
```

Verdict (recorded in `defect_review.json`):

- `verdict`: `REVIEWED`
- `is_true_positive`: `false` — **false positive**
- `failure_reason`: `other`
- `needs_dfg`: `true`
- `missing_capability`: `跨方法/经出参的中间 def（intervening def via callee out-param）`
- `confidence`: `0.7`
- `reason`: `` src/redis-benchmark.c:1760 的 WRITE 是 `config.cluster_nodes = NULL`（初始置空）；:1803 的解引用在 `for (j = 0; j < config.cluster_node_count; j++)` 内——count>0 蕴含解析阶段已在别处把 cluster_nodes 重新赋值为分配结果。杀死 NULL def 的 intervening def 经跨方法/字段写入，图未连出这条 def；补上「真实分配 def → 解引用」的数据流边后此候选即被击杀。 ``
- `annotator`: `AI 提议 + 人工确认（Claude Code 提议 / Kyber5323 逐条确认，2026-09-24）`
- `notes`: `null`

### DFV1-llama.cpp-69-9fc4d428cfd8c4b8

- type: `3.1` / `null_flow`
- status: `ambiguous` (confidence 0.3)
- subject: `c` in `apple_rdma.probe`
- candidate: `E-DEFECT-a0b32609f164db1c37813dfc`
- anchor: `event:b5254c1c3972df6218d21245814b98baa1addd499a750c27cf4014f251b0fc71`
- source: `ggml/src/ggml-rpc/transport-apple.cpp:268-268`, `ggml/src/ggml-rpc/transport-apple.cpp:283-283`

Facts:

- `event`='ALLOC', `matched_name`='posix_memalign', `method`='apple_rdma.probe', `point`='event:9f460fcf46128cc2e294d13c23941201085ef27aa62796e2f82f18aab19acbf7', `source`='ggml/src/ggml-rpc/transport-apple.cpp:268-268', `subject`='c', `subject_from`='argument'
- `event`='DEREFERENCE', `matched_name`='c->qpn', `method`='apple_rdma.probe', `point`='event:b5254c1c3972df6218d21245814b98baa1addd499a750c27cf4014f251b0fc71', `source`='ggml/src/ggml-rpc/transport-apple.cpp:283-283', `subject`='c', `subject_from`=''

Uncertain facts:

- `decisive`=True, `detail`='a check on this identity exists but its null arm still reaches the dereference, so some route to it is unguarded', `fact`='PARTIAL_GUARD', `point`='event:a3203645d62b97487f9b17fc6da3fa5a6293ea80b80b04c3b39fa15e06be6282', `source`='ggml/src/ggml-rpc/transport-apple.cpp:215-215', `status`='ambiguous'

Missing evidence:

- which calls can return NULL: the graph sees the allocation and the dereference, and whether this call's failure path is reachable at all is an API contract the AI side holds
- whether a guard covers every route to the dereference: the graph can say the null arm still reaches it, not whether some check upstream already excluded null on the paths that matter

```cpp
    const size_t ring_bytes = (size_t)RDMA_NBUF * RDMA_STRIDE;
    if (posix_memalign((void **)&c->send_mem, (size_t)page, ring_bytes) != 0) c->send_mem = nullptr;
    if (posix_memalign((void **)&c->recv_mem, (size_t)page, ring_bytes) != 0) c->recv_mem = nullptr;
    if (!c->send_mem || !c->recv_mem) return nullptr;

```

```cpp

    apple_rdma_caps rc = {};
    rc.qpn = c->qpn;
    rc.lid = pa.lid;
    memcpy(rc.gid, gid.raw, RDMA_GID_SIZE);
```

Verdict (recorded in `defect_review.json`):

- `verdict`: `REVIEWED`
- `is_true_positive`: `false` — **false positive**
- `failure_reason`: `extractor`
- `needs_dfg`: `false`
- `missing_capability`: `出参分配器契约（out-param allocator: posix_memalign）`
- `confidence`: `0.8`
- `reason`: `` ggml/src/ggml-rpc/transport-apple.cpp:268 的 ALLOC 事实把 `posix_memalign(&c->send_mem, …)` 当作 malloc 式工厂——posix_memalign 经出参分配、返回错误码，被追踪的「分配值」并不是流向 :283 `c->qpn` 解引用的指针；`c` 本身在 :215 已判空返回（PARTIAL_GUARD 已如实标注）。 ``
- `annotator`: `AI 提议 + 人工确认（Claude Code 提议 / Kyber5323 逐条确认，2026-09-24）`
- `notes`: `null`

### DFV1-redis-50-9e263c54611a14ba

- type: `3.1` / `null_flow`
- status: `resolved` (confidence 0.5)
- subject: `entries` in `main`
- candidate: `E-DEFECT-68f1843b6a33d95c7981672a`
- anchor: `event:6cf65ff220f0925e2b23dcb7b6bb8b7279b372dc480be17752363d2bf29fb010`
- source: `utils/lru/lfu-simulation.c:87-87`, `utils/lru/lfu-simulation.c:135-135`

Facts:

- `event`='ALLOC', `matched_name`='malloc', `method`='main', `point`='event:7b15e140499a94c314649ad2fa7928972da4399b06eb3f7b2fca77373169c81c', `source`='utils/lru/lfu-simulation.c:87-87', `subject`='entries', `subject_from`='assignment'
- `event`='DEREFERENCE', `matched_name`='entries[idx]', `method`='main', `point`='event:6cf65ff220f0925e2b23dcb7b6bb8b7279b372dc480be17752363d2bf29fb010', `source`='utils/lru/lfu-simulation.c:135-135', `subject`='entries', `subject_from`=''

Uncertain facts:


Missing evidence:

- which calls can return NULL: the graph sees the allocation and the dereference, and whether this call's failure path is reachable at all is an API contract the AI side holds
- whether a guard covers every route to the dereference: the graph can say the null arm still reaches it, not whether some check upstream already excluded null on the paths that matter

```c
    time_t new_entry_time = start;
    time_t display_time = start;
    struct entry *entries = malloc(sizeof(*entries)*keyspace_size);
    long j;

```

```c
            idx = 10+(rand()%10);
            entries[idx].counter = LFU_INIT_VAL;
            entries[idx].decrtime = to_16bit_minutes(time(NULL));
            entries[idx].hits = 0;
            entries[idx].ctime = time(NULL);
```

Verdict (recorded in `defect_review.json`):

- `verdict`: `REVIEWED`
- `is_true_positive`: `true` — **true positive**
- `failure_reason`: `null`
- `needs_dfg`: `false`
- `missing_capability`: `null`
- `confidence`: `0.9`
- `reason`: `` utils/lru/lfu-simulation.c:87 `entries = malloc(sizeof(*entries) * …)` 的返回值全程无任何 CHECK（resolved 判定与源码一致），:135 `entries[idx].counter` 直接解引用——典型 3.1：分配失败即空指针解引用。 ``
- `annotator`: `AI 提议 + 人工确认（Claude Code 提议 / Kyber5323 逐条确认，2026-09-24）`
- `notes`: `本单的真阳性样本；仿真工具程序，但缺陷模式典型。`

### DFV1-llama.cpp-69-19a31c006f46316e

- type: `3.1` / `null_flow`
- status: `resolved` (confidence 0.5)
- subject: `galloc` in `ggml_gallocr_new_n`
- candidate: `E-DEFECT-3c741ac346ad8e72ec2413c9`
- anchor: `event:e352897c598f1eabfe63bee36d6fb10e4e2527b4fcebdafb8d811565d76deff8`
- source: `ggml/src/ggml-alloc.c:499-499`, `ggml/src/ggml-alloc.c:503-503`

Facts:

- `event`='ALLOC', `matched_name`='calloc', `method`='ggml_gallocr_new_n', `point`='event:88888e7ad1c069d2fbb9d89f8f67d4f34c527aa0ec3c737ae6bc1fa7119ca504', `source`='ggml/src/ggml-alloc.c:499-499', `subject`='galloc', `subject_from`='assignment'
- `event`='DEREFERENCE', `matched_name`='galloc->bufts', `method`='ggml_gallocr_new_n', `point`='event:e352897c598f1eabfe63bee36d6fb10e4e2527b4fcebdafb8d811565d76deff8', `source`='ggml/src/ggml-alloc.c:503-503', `subject`='galloc', `subject_from`=''

Uncertain facts:


Missing evidence:

- which calls can return NULL: the graph sees the allocation and the dereference, and whether this call's failure path is reachable at all is an API contract the AI side holds
- whether a guard covers every route to the dereference: the graph can say the null arm still reaches it, not whether some check upstream already excluded null on the paths that matter

```c

ggml_gallocr_t ggml_gallocr_new_n(ggml_backend_buffer_type_t * bufts, int n_bufs) {
    ggml_gallocr_t galloc = (ggml_gallocr_t)calloc(1, sizeof(struct ggml_gallocr));
    GGML_ASSERT(galloc != NULL);

```

```c

    galloc->bufts = calloc(n_bufs, sizeof(ggml_backend_buffer_type_t));
    GGML_ASSERT(galloc->bufts != NULL);

    galloc->buffers = calloc(n_bufs, sizeof(struct vbuffer *));
```

Verdict (recorded in `defect_review.json`):

- `verdict`: `REVIEWED`
- `is_true_positive`: `false` — **false positive**
- `failure_reason`: `extractor`
- `needs_dfg`: `false`
- `missing_capability`: `宏内 null 断言识别（GGML_ASSERT）`
- `confidence`: `0.85`
- `reason`: `` ggml/src/ggml-alloc.c:499 calloc 后 :500 紧跟 `GGML_ASSERT(galloc != NULL)`——断言失败即进程终止，解引用不可达；宏包裹的 null 比较未被 CHECK 识别（CHECK 只认裸比较表达式），导致「该身份无 CHECK」的 resolved 误立。 ``
- `annotator`: `AI 提议 + 人工确认（Claude Code 提议 / Kyber5323 逐条确认，2026-09-24）`
- `notes`: `null`

### DFV1-redis-50-84f9fb6a1311ff90

- type: `4.1` / `resource_lifetime`
- status: `ambiguous` (confidence 0.4)
- subject: `word` in `threaded_insert`
- candidate: `E-DEFECT-90d0e4f96ceecb0e745c3d35`
- anchor: `event:7f09e8aa6d5df81efbd65555bc4b30d837930c6c60170b3890c10df517fb4b67`
- source: `modules/vector-sets/w2v.c:356-356`, `modules/vector-sets/w2v.c:357-357`, `modules/vector-sets/w2v.c:362-362`, `modules/vector-sets/w2v.c:363-363`, `modules/vector-sets/w2v.c:372-372`, `modules/vector-sets/w2v.c:373-373`, `modules/vector-sets/w2v.c:374-374`, `modules/vector-sets/w2v.c:379-379`, `modules/vector-sets/w2v.c:383-383`, `modules/vector-sets/w2v.c:380-380`, `modules/vector-sets/w2v.c:352-352`, `modules/vector-sets/w2v.c:376-376`, `modules/vector-sets/w2v.c:381-381`, `modules/vector-sets/w2v.c:364-364`, `modules/vector-sets/w2v.c:365-365`, `modules/vector-sets/w2v.c:358-358`, `modules/vector-sets/w2v.c:359-359`

Facts:

- `event`='ALLOC', `matched_name`='malloc', `method`='threaded_insert', `point`='event:7f09e8aa6d5df81efbd65555bc4b30d837930c6c60170b3890c10df517fb4b67', `source`='modules/vector-sets/w2v.c:356-356', `subject`='word', `subject_from`='assignment'
- `event`='CALL', `matched_name`='call_expression', `method`='threaded_insert', `point`='event:e0dc6e7907118e4002c44ce98e9b7e7f2183f2fac919df049c717e5335da6c9f', `source`='modules/vector-sets/w2v.c:356-356', `subject`='word', `subject_from`='assignment'
- `event`='DEREFERENCE', `matched_name`='ctx->fp', `method`='threaded_insert', `point`='event:546c67b42ba708bd4d0467d1afe7a617fb9293230a2a95ef259575912e201a97', `source`='modules/vector-sets/w2v.c:357-357', `subject`='ctx', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='threaded_insert', `point`='event:6984d4c7003b4889b1e870ae17cc4414564ea8ac8d2653f100c059210d702dff', `source`='modules/vector-sets/w2v.c:357-357', `subject`='word', `subject_from`='argument'
- `event`='BRANCH', `matched_name`='fread(word,wlen,1,ctx->fp)<=0', `method`='threaded_insert', `point`='event:c9d8a2eef45e66e489815f3501f24306f26e74b4d2ad3b597f152dadbcb2a3b0', `source`='modules/vector-sets/w2v.c:357-357', `subject`='', `subject_from`=''
- `event`='DEREFERENCE', `matched_name`='word[wlen]', `method`='threaded_insert', `point`='event:41522df1d5217a6c43f6c3c6c2b9f70ee7984cf129f31ac35e6b9e255acaa9bc', `source`='modules/vector-sets/w2v.c:362-362', `subject`='word', `subject_from`=''
- `event`='DEREFERENCE', `matched_name`='ctx->fp', `method`='threaded_insert', `point`='event:5fccfaf62716c6bb260463e7975f2cdb46b013acbeb7cba2d01f8238bf6c5aeb', `source`='modules/vector-sets/w2v.c:363-363', `subject`='ctx', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='threaded_insert', `point`='event:0b273ad2c5484cfff89025b8aeeb785e3a0988e39d6b3eed0d55d6908bbf6bc1', `source`='modules/vector-sets/w2v.c:363-363', `subject`='v', `subject_from`='argument'
- `event`='BRANCH', `matched_name`='fread(v,300*sizeof(float),1,ctx->fp)<=0', `method`='threaded_insert', `point`='event:a1643d43780c2e11fcb731f8bacc2ea6e3dfcc8a4f5e8e9cb5914986df7074c1', `source`='modules/vector-sets/w2v.c:363-363', `subject`='', `subject_from`=''
- `event`='DEREFERENCE', `matched_name`='ctx->id', `method`='threaded_insert', `point`='event:02262ac160f532b02fad38f1f4ead2f4a86497528da2a894ebe49a0de3350bf1', `source`='modules/vector-sets/w2v.c:372-372', `subject`='ctx', `subject_from`=''
- `event`='DEREFERENCE', `matched_name`='ctx->index', `method`='threaded_insert', `point`='event:31e0ec67822fe6f4fa2bb05c917798e7469cf4b8792c5041e0b30fcb263036ac', `source`='modules/vector-sets/w2v.c:373-373', `subject`='ctx', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='threaded_insert', `point`='event:533117a6cb594016ade30520e13ee3307ede109b76ff69eb38716941d4f5c55e', `source`='modules/vector-sets/w2v.c:373-373', `subject`='ic', `subject_from`='assignment'
- `event`='DEREFERENCE', `matched_name`='ctx->index', `method`='threaded_insert', `point`='event:c26f5908145b5aa1e0a7af9449d738a04b139d36b7777fbb94aa8a1595614868', `source`='modules/vector-sets/w2v.c:374-374', `subject`='ctx', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='threaded_insert', `point`='event:a7c9160c7d85c8fcbed3c0973f00ed94ab16430d07000a58ce0abe35253d741a', `source`='modules/vector-sets/w2v.c:374-374', `subject`='ctx', `subject_from`='argument'
- `event`='CHECK', `matched_name`='hnsw_try_commit_insert(ctx->index,ic,word)==NULL', `method`='threaded_insert', `point`='event:0775eeb8ace00347055c3b565cb7bb3481324bb34f403740989ded1e46e26dcf', `source`='modules/vector-sets/w2v.c:374-374', `subject`='', `subject_from`=''
- `event`='DEREFERENCE', `matched_name`='ctx->numele', `method`='threaded_insert', `point`='event:1f8a9b53c9719f4c4a27d9fca93b18db2dec57e364e3b33acdc91f7770a80409', `source`='modules/vector-sets/w2v.c:379-379', `subject`='ctx', `subject_from`=''
- `event`='BRANCH', `matched_name`='next_id>=ctx->numele', `method`='threaded_insert', `point`='event:0dfbd660dff5b7a392e53846d2f8b6d3c8e9fa87ecece3758b1baa5f07efeef4', `source`='modules/vector-sets/w2v.c:379-379', `subject`='', `subject_from`=''
- `event`='RETURN', `matched_name`='return_statement', `method`='threaded_insert', `point`='event:39aba7c24d19e68df2d5cffadc164756e87e5a2e62987f4a32219b7b974487e4', `source`='modules/vector-sets/w2v.c:383-383', `subject`='', `subject_from`=''
- `event`='CHECK', `matched_name`='!((next_id+1)%10000)', `method`='threaded_insert', `point`='event:f5e8bad68a0f3ac8469aa042638322d1ffdcb4f61a276ac54c7b2ede8ddb5c80', `source`='modules/vector-sets/w2v.c:380-380', `subject`='', `subject_from`=''
- `event`='BRANCH', `matched_name`='1', `method`='threaded_insert', `point`='event:3cc0003f0a5b7fa40d944a67b7c1ffa651442755699bd5ca964df30dd6214660', `source`='modules/vector-sets/w2v.c:352-352', `subject`='', `subject_from`=''
- `event`='DEREFERENCE', `matched_name`='ctx->index', `method`='threaded_insert', `point`='event:9115650baf6a5ceeed7d97df203c3c28db441320af37ec516b10287c19c1eabb', `source`='modules/vector-sets/w2v.c:376-376', `subject`='ctx', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='threaded_insert', `point`='event:b58ad1f7b11fdc433050aabdb5d817640ddb7531f88d1a606539af40fe515c65', `source`='modules/vector-sets/w2v.c:376-376', `subject`='ctx', `subject_from`='argument'
- `event`='CALL', `matched_name`='call_expression', `method`='threaded_insert', `point`='event:c3645f023d65e01fb33d040ee8a0033ed48bac8059159f220e0f3bff5183d4c3', `source`='modules/vector-sets/w2v.c:381-381', `subject`='', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='threaded_insert', `point`='event:b01d1cf5cd8c577caa93066898c665134ce21e580cb4a9e03657e8f56d1b0368', `source`='modules/vector-sets/w2v.c:364-364', `subject`='', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='threaded_insert', `point`='event:0609e34cbe69fdc0f155f4547047ad502c292283345157a2e589fc2ed9c55540', `source`='modules/vector-sets/w2v.c:365-365', `subject`='', `subject_from`=''
- `event`='THROW', `matched_name`='exit', `method`='threaded_insert', `point`='event:42035399feba07ff5d2159882206ca7716dc509900eddef012bd97f41ee7565a', `source`='modules/vector-sets/w2v.c:365-365', `subject`='', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='threaded_insert', `point`='event:9a7a0813df12ef90645b81152273c526a55edb3b1b0ab7d93ba9a70702fe36d8', `source`='modules/vector-sets/w2v.c:358-358', `subject`='', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='threaded_insert', `point`='event:b74fdc1f0e635f580da70023803745277ba6bd5a0c5428ecb4feb41a2b26c724', `source`='modules/vector-sets/w2v.c:359-359', `subject`='', `subject_from`=''
- `event`='THROW', `matched_name`='exit', `method`='threaded_insert', `point`='event:6bb73b86683932ea99af495a7524e25fd6a1235c79bffc28357f560d41406244', `source`='modules/vector-sets/w2v.c:359-359', `subject`='', `subject_from`=''

Uncertain facts:

- `decisive`=True, `detail`="this name is passed to a callee, which may take ownership (as the call's own subject)", `fact`='OWNERSHIP_TRANSFER', `point`='event:6984d4c7003b4889b1e870ae17cc4414564ea8ac8d2653f100c059210d702dff', `source`='modules/vector-sets/w2v.c:357-357', `status`='ambiguous'
- `decisive`=True, `detail`="this name is passed to a callee, which may take ownership (as an argument named 'word', matched by spelling)", `fact`='OWNERSHIP_TRANSFER', `point`='event:a7c9160c7d85c8fcbed3c0973f00ed94ab16430d07000a58ce0abe35253d741a', `source`='modules/vector-sets/w2v.c:374-374', `status`='ambiguous'
- `decisive`=True, `detail`="this name is passed to a callee, which may take ownership (as an argument named 'word', matched by spelling)", `fact`='OWNERSHIP_TRANSFER', `point`='event:b58ad1f7b11fdc433050aabdb5d817640ddb7531f88d1a606539af40fe515c65', `source`='modules/vector-sets/w2v.c:376-376', `status`='ambiguous'

Missing evidence:

- the ownership contract of a callee: whether a function the resource is passed to takes ownership, borrows it, or frees it
- RAII and smart pointers: `unique_ptr<T> p(new T)` is an ALLOC with no RELEASE in the graph, so every owning smart pointer reads as a leak
- aliasing and reassignment: `q = p; free(q)`, `free(p); p = NULL` and `p = realloc(p, n)` are all invisible without a data-flow graph
- a release performed inside a callee: the graph only sees RELEASE points in the method it walked

```c
        if (fread(&wlen,2,1,ctx->fp) == 0) break;
        pthread_mutex_unlock(&ctx->FileAccessMutex);
        word = malloc(wlen+1);
        if (fread(word,wlen,1,ctx->fp) <= 0) {
            perror("Unexpected EOF");
```

```c
        pthread_mutex_unlock(&ctx->FileAccessMutex);
        word = malloc(wlen+1);
        if (fread(word,wlen,1,ctx->fp) <= 0) {
            perror("Unexpected EOF");
            exit(1);
```

```c
        }

        word[wlen] = 0;
        if (fread(v,300*sizeof(float),1,ctx->fp) <= 0) {
            perror("Unexpected EOF");
```

```c

        word[wlen] = 0;
        if (fread(v,300*sizeof(float),1,ctx->fp) <= 0) {
            perror("Unexpected EOF");
            exit(1);
```

```c
        // applies the check if the graph wasn't modified.
        InsertContext *ic;
        uint64_t next_id = ctx->id++;
        ic = hnsw_prepare_insert(ctx->index, v, NULL, 0, next_id, 200);
        if (hnsw_try_commit_insert(ctx->index, ic, word) == NULL) {
```

```c
        InsertContext *ic;
        uint64_t next_id = ctx->id++;
        ic = hnsw_prepare_insert(ctx->index, v, NULL, 0, next_id, 200);
        if (hnsw_try_commit_insert(ctx->index, ic, word) == NULL) {
            // This time try locking since the start.
```

```c
        uint64_t next_id = ctx->id++;
        ic = hnsw_prepare_insert(ctx->index, v, NULL, 0, next_id, 200);
        if (hnsw_try_commit_insert(ctx->index, ic, word) == NULL) {
            // This time try locking since the start.
            hnsw_insert(ctx->index, v, NULL, 0, next_id, word, 200);
```

```c
        }

        if (next_id >= ctx->numele) break;
        if (!((next_id+1) % 10000))
            printf("%llu added\n", (unsigned long long)next_id+1);
```

```c
            printf("%llu added\n", (unsigned long long)next_id+1);
    }
    return NULL;
}

```

```c

        if (next_id >= ctx->numele) break;
        if (!((next_id+1) % 10000))
            printf("%llu added\n", (unsigned long long)next_id+1);
    }
```

```c
    uint16_t wlen;

    while(1) {
        pthread_mutex_lock(&ctx->FileAccessMutex);
        if (fread(&wlen,2,1,ctx->fp) == 0) break;
```

```c
        if (hnsw_try_commit_insert(ctx->index, ic, word) == NULL) {
            // This time try locking since the start.
            hnsw_insert(ctx->index, v, NULL, 0, next_id, word, 200);
        }

```

```c
        if (next_id >= ctx->numele) break;
        if (!((next_id+1) % 10000))
            printf("%llu added\n", (unsigned long long)next_id+1);
    }
    return NULL;
```

```c
        word[wlen] = 0;
        if (fread(v,300*sizeof(float),1,ctx->fp) <= 0) {
            perror("Unexpected EOF");
            exit(1);
        }
```

```c
        if (fread(v,300*sizeof(float),1,ctx->fp) <= 0) {
            perror("Unexpected EOF");
            exit(1);
        }

```

```c
        word = malloc(wlen+1);
        if (fread(word,wlen,1,ctx->fp) <= 0) {
            perror("Unexpected EOF");
            exit(1);
        }
```

```c
        if (fread(word,wlen,1,ctx->fp) <= 0) {
            perror("Unexpected EOF");
            exit(1);
        }

```

Verdict (recorded in `defect_review.json`):

- `verdict`: `REVIEWED`
- `is_true_positive`: `false` — **false positive**
- `failure_reason`: `contract`
- `needs_dfg`: `false`
- `missing_capability`: `API 所有权契约（callee takes ownership）`
- `confidence`: `0.75`
- `reason`: `` modules/vector-sets/w2v.c:356 循环内 `word = malloc(…)` 随后交给 hnsw_try_commit_insert(ctx->index, ic, word) / hnsw_insert(…, word, …)——vector-sets 的 hnsw_insert 接管 blob 所有权并随节点存入索引；fread 失败路径 exit(1)。每条路径 word 都移交出帧，无泄漏。 ``
- `annotator`: `AI 提议 + 人工确认（Claude Code 提议 / Kyber5323 逐条确认，2026-09-24）`
- `notes`: `null`

### DFV1-llama.cpp-69-0e72cd2816ec69e4

- type: `4.1` / `resource_lifetime`
- status: `ambiguous` (confidence 0.4)
- subject: `ss` in `str_to_vec`
- candidate: `E-DEFECT-c2f3576e5cebaa95c5285fc2`
- anchor: `event:f64d6b0929fbc84cb38d91e4eb1c2a9f1ac094130a3c9df511b2a264efca2621`
- source: `ggml/src/ggml-hexagon/ggml-hexagon.cpp:7137-7137`, `ggml/src/ggml-hexagon/ggml-hexagon.cpp:7141-7141`

Facts:

- `event`='ALLOC', `matched_name`='getline', `method`='str_to_vec', `point`='event:f64d6b0929fbc84cb38d91e4eb1c2a9f1ac094130a3c9df511b2a264efca2621', `source`='ggml/src/ggml-hexagon/ggml-hexagon.cpp:7137-7137', `subject`='ss', `subject_from`='argument'
- `event`='CALL', `matched_name`='call_expression', `method`='str_to_vec', `point`='event:7d846d12658c9981c4cc84bac5faa70c5ada9db1577efa3109f412385d15da46', `source`='ggml/src/ggml-hexagon/ggml-hexagon.cpp:7137-7137', `subject`='ss', `subject_from`='argument'
- `event`='BRANCH', `matched_name`="std::getline(ss,t,',')", `method`='str_to_vec', `point`='event:bdca5d1576f2caa035d4161606448e41c7a4a4cd2f93583b242330bf45f1a855', `source`='ggml/src/ggml-hexagon/ggml-hexagon.cpp:7137-7137', `subject`='', `subject_from`=''
- `event`='RETURN', `matched_name`='return_statement', `method`='str_to_vec', `point`='event:f988e95c0fa368db0560c598e424732cc97959693c7ec6ba0a677f5389ab3e9b', `source`='ggml/src/ggml-hexagon/ggml-hexagon.cpp:7141-7141', `subject`='v', `subject_from`='return'

Uncertain facts:

- `decisive`=True, `detail`="this name is passed to a callee, which may take ownership (as the call's own subject)", `fact`='OWNERSHIP_TRANSFER', `point`='event:7d846d12658c9981c4cc84bac5faa70c5ada9db1577efa3109f412385d15da46', `source`='ggml/src/ggml-hexagon/ggml-hexagon.cpp:7137-7137', `status`='ambiguous'

Missing evidence:

- the ownership contract of a callee: whether a function the resource is passed to takes ownership, borrows it, or frees it
- RAII and smart pointers: `unique_ptr<T> p(new T)` is an ALLOC with no RELEASE in the graph, so every owning smart pointer reads as a leak
- aliasing and reassignment: `q = p; free(q)`, `free(p); p = NULL` and `p = realloc(p, n)` are all invisible without a data-flow graph
- a release performed inside a callee: the graph only sees RELEASE points in the method it walked

```cpp
    std::string    t;

    while (std::getline(ss, t, ',')) {
        v.push_back(std::stoul(t, nullptr, 0));
    }
```

```cpp
    }

    return v;
}

```

Verdict (recorded in `defect_review.json`):

- `verdict`: `REVIEWED`
- `is_true_positive`: `false` — **false positive**
- `failure_reason`: `extractor`
- `needs_dfg`: `false`
- `missing_capability`: `getline 误入 ALLOC 词表（std::getline misclassified）`
- `confidence`: `0.9`
- `reason`: `` ggml/src/ggml-hexagon/ggml-hexagon.cpp:7137 的 ALLOC 事实 matched_name=getline——`std::getline(ss, t, ',')` 是读流不是分配；ss 是栈上 RAII stringstream，无堆资源。提取词表把 getline 误映射为 ALLOC。 ``
- `annotator`: `AI 提议 + 人工确认（Claude Code 提议 / Kyber5323 逐条确认，2026-09-24）`
- `notes`: `null`

### DFV1-redis-50-d57419b7efe1c7e9

- type: `4.1` / `resource_lifetime`
- status: `resolved` (confidence 0.6)
- subject: `tmp` in `spt_copyargs`
- candidate: `E-DEFECT-06f1abf748c9c7afd180581e`
- anchor: `event:4605fcbc4476688688d212f205508ed86079fc9e0f38c3db016c6b2d6a2687a5`
- source: `src/setproctitle.c:172-172`, `src/setproctitle.c:173-173`, `src/setproctitle.c:175-175`, `src/setproctitle.c:168-168`, `src/setproctitle.c:178-178`

Facts:

- `event`='ALLOC', `matched_name`='strdup', `method`='spt_copyargs', `point`='event:4605fcbc4476688688d212f205508ed86079fc9e0f38c3db016c6b2d6a2687a5', `source`='src/setproctitle.c:172-172', `subject`='tmp', `subject_from`='assignment'
- `event`='CALL', `matched_name`='call_expression', `method`='spt_copyargs', `point`='event:63654e925ccfa052fd7884eb82dc2636283ba4fef5d7483195bd7609101d7400', `source`='src/setproctitle.c:172-172', `subject`='tmp', `subject_from`='assignment'
- `event`='CHECK', `matched_name`='!(tmp=strdup(argv[i]))', `method`='spt_copyargs', `point`='event:8abfe7bbbba7ae159f6aa06bb43db85349b879c64832850c5fa25f030d914ddd', `source`='src/setproctitle.c:172-172', `subject`='', `subject_from`=''
- `event`='RETURN', `matched_name`='return_statement', `method`='spt_copyargs', `point`='event:52045ec3ddcc2295c4afe13f55f907574d2a561ba8b599e0b4e58576268e7163', `source`='src/setproctitle.c:173-173', `subject`='errno', `subject_from`='return'
- `event`='DEREFERENCE', `matched_name`='argv[i]', `method`='spt_copyargs', `point`='event:422f35abde6282e447828e5038810832d81b4cd60dfa45c7e26d6a23b188c782', `source`='src/setproctitle.c:175-175', `subject`='argv', `subject_from`=''
- `event`='DEREFERENCE', `matched_name`='argv[i]', `method`='spt_copyargs', `point`='event:a1bb0d4f9391635db5a18ecaecb5953ef1aaddef7d41533f972389c41c77e391', `source`='src/setproctitle.c:168-168', `subject`='argv', `subject_from`=''
- `event`='BRANCH', `matched_name`='i<argc||(i>=argc&&argv[i])', `method`='spt_copyargs', `point`='event:4337f30a4dd1d26543ec17ce4b5b7675f842ca5caeb4781f9ece7a31e0ce0504', `source`='src/setproctitle.c:168-168', `subject`='', `subject_from`=''
- `event`='RETURN', `matched_name`='return_statement', `method`='spt_copyargs', `point`='event:d3a921f6e1d5f94c24a2b8028b0a1c09cf5bd10d467dfa3d0c8511383d8d658b', `source`='src/setproctitle.c:178-178', `subject`='', `subject_from`=''

Uncertain facts:


Missing evidence:

- the ownership contract of a callee: whether a function the resource is passed to takes ownership, borrows it, or frees it
- RAII and smart pointers: `unique_ptr<T> p(new T)` is an ALLOC with no RELEASE in the graph, so every owning smart pointer reads as a leak
- aliasing and reassignment: `q = p; free(q)`, `free(p); p = NULL` and `p = realloc(p, n)` are all invisible without a data-flow graph
- a release performed inside a callee: the graph only sees RELEASE points in the method it walked

```c
			continue;

		if (!(tmp = strdup(argv[i])))
			return errno;

```

```c

		if (!(tmp = strdup(argv[i])))
			return errno;

		argv[i] = tmp;
```

```c
			return errno;

		argv[i] = tmp;
	}

```

```c
	int i;

	for (i = 1; i < argc || (i >= argc && argv[i]); i++) {
		if (!argv[i])
			continue;
```

```c
	}

	return 0;
} /* spt_copyargs() */

```

Verdict (recorded in `defect_review.json`):

- `verdict`: `REVIEWED`
- `is_true_positive`: `false` — **false positive**
- `failure_reason`: `ownership`
- `needs_dfg`: `false`
- `missing_capability`: `经参数数组的移交（store-through-parameter transfer）`
- `confidence`: `0.85`
- `reason`: `` src/setproctitle.c:172 `tmp = strdup(argv[i])` 后 :176 `argv[i] = tmp`——strdup 结果移交进调用方的 argv 数组，由 setproctitle 的调用方统一释放；图只见帧内无 RELEASE 即立候选。 ``
- `annotator`: `AI 提议 + 人工确认（Claude Code 提议 / Kyber5323 逐条确认，2026-09-24）`
- `notes`: `null`

### DFV1-llama.cpp-69-536a4d3fd1731572

- type: `4.1` / `resource_lifetime`
- status: `resolved` (confidence 0.6)
- subject: `dev_ctx` in `ggml_backend_rpc_add_server`
- candidate: `E-DEFECT-89f53c2728ffbc9b9579d5ea`
- anchor: `event:6afff70d273908384daf018dcd87aa7487cc994c0b9c69e9fa0407ef0f6b185c`
- source: `ggml/src/ggml-rpc/ggml-rpc.cpp:2348-2354`, `ggml/src/ggml-rpc/ggml-rpc.cpp:2358-2358`, `ggml/src/ggml-rpc/ggml-rpc.cpp:2356-2360`, `ggml/src/ggml-rpc/ggml-rpc.cpp:2361-2361`, `ggml/src/ggml-rpc/ggml-rpc.cpp:2345-2345`, `ggml/src/ggml-rpc/ggml-rpc.cpp:2364-2368`, `ggml/src/ggml-rpc/ggml-rpc.cpp:2369-2369`, `ggml/src/ggml-rpc/ggml-rpc.cpp:2370-2370`

Facts:

- `event`='ALLOC', `matched_name`='new_expression', `method`='ggml_backend_rpc_add_server', `point`='event:6afff70d273908384daf018dcd87aa7487cc994c0b9c69e9fa0407ef0f6b185c', `source`='ggml/src/ggml-rpc/ggml-rpc.cpp:2348-2354', `subject`='dev_ctx', `subject_from`='assignment'
- `event`='CALL', `matched_name`='call_expression', `method`='ggml_backend_rpc_add_server', `point`='event:cb28ce11acce658f75cf63f2c17fe70a0ff069b97c9d6f954ed6ab6789eaa409', `source`='ggml/src/ggml-rpc/ggml-rpc.cpp:2358-2358', `subject`='dev', `subject_from`='assignment'
- `event`='ALLOC', `matched_name`='new_expression', `method`='ggml_backend_rpc_add_server', `point`='event:e335bec82aedc2e766057d9b353c8181b5b081d81e6ee7ebfd457011f2c64628', `source`='ggml/src/ggml-rpc/ggml-rpc.cpp:2356-2360', `subject`='dev', `subject_from`='assignment'
- `event`='DEREFERENCE', `matched_name`='ctx->devices', `method`='ggml_backend_rpc_add_server', `point`='event:4cc789987bbee98c925e0956b06e65a68ed57af02a66022a668c311c9ca005fc', `source`='ggml/src/ggml-rpc/ggml-rpc.cpp:2361-2361', `subject`='ctx', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='ggml_backend_rpc_add_server', `point`='event:0494b87fc258037170ee0bb2733818ed75dc0b376b788994e0458403cfe176a8', `source`='ggml/src/ggml-rpc/ggml-rpc.cpp:2361-2361', `subject`='dev', `subject_from`='argument'
- `event`='BRANCH', `matched_name`='ind<dev_count', `method`='ggml_backend_rpc_add_server', `point`='event:48803e6be39b4848036eb0263ffb6abc86f20cb6689ee7f4caa422a7d0c1b856', `source`='ggml/src/ggml-rpc/ggml-rpc.cpp:2345-2345', `subject`='', `subject_from`=''
- `event`='ALLOC', `matched_name`='new_expression', `method`='ggml_backend_rpc_add_server', `point`='event:b38ac45c505473c91f32be4bf5fda6a2615f57dea4955e4275ba8a7e1ea4f700', `source`='ggml/src/ggml-rpc/ggml-rpc.cpp:2364-2368', `subject`='reg', `subject_from`='assignment'
- `event`='DEREFERENCE', `matched_name`='reg_map[endpoint]', `method`='ggml_backend_rpc_add_server', `point`='event:3402a02037cf6a8bd382baddb9646c0c0b836bb627cf19279a0c12dbbe226ca7', `source`='ggml/src/ggml-rpc/ggml-rpc.cpp:2369-2369', `subject`='reg_map', `subject_from`=''
- `event`='RETURN', `matched_name`='return_statement', `method`='ggml_backend_rpc_add_server', `point`='event:672832ed5ce48306a120999d93d89e4e781441b90849a107fe24ae189d830be1', `source`='ggml/src/ggml-rpc/ggml-rpc.cpp:2370-2370', `subject`='reg', `subject_from`='return'

Uncertain facts:


Missing evidence:

- the ownership contract of a callee: whether a function the resource is passed to takes ownership, borrows it, or frees it
- RAII and smart pointers: `unique_ptr<T> p(new T)` is an ALLOC with no RELEASE in the graph, so every owning smart pointer reads as a leak
- aliasing and reassignment: `q = p; free(q)`, `free(p); p = NULL` and `p = realloc(p, n)` are all invisible without a data-flow graph
- a release performed inside a callee: the graph only sees RELEASE points in the method it walked

```cpp
        std::string dev_name = "RPC" + std::to_string(dev_id);
        std::string dev_desc = std::string(endpoint);
        ggml_backend_rpc_device_context * dev_ctx = new ggml_backend_rpc_device_context {
            /* .endpoint    = */    endpoint,
            /* .device      = */    ind,
            /* .name        = */    dev_name,
            /* .description = */    dev_desc,
            /* .last_graph_uid = */ 0,
        };

        ggml_backend_dev_t dev = new ggml_backend_device {
```

```cpp
        ggml_backend_dev_t dev = new ggml_backend_device {
            /* .iface   = */ ggml_backend_rpc_device_i,
            /* .reg     = */ ggml_backend_rpc_reg(),
            /* .context = */ dev_ctx,
        };
```

```cpp
        };

        ggml_backend_dev_t dev = new ggml_backend_device {
            /* .iface   = */ ggml_backend_rpc_device_i,
            /* .reg     = */ ggml_backend_rpc_reg(),
            /* .context = */ dev_ctx,
        };
        ctx->devices.push_back(dev);
        dev_id++;
```

```cpp
            /* .context = */ dev_ctx,
        };
        ctx->devices.push_back(dev);
        dev_id++;
    }
```

```cpp
    ggml_backend_rpc_reg_context * ctx = new ggml_backend_rpc_reg_context;
    ctx->name = "RPC[" + std::string(endpoint) + "]";
    for (uint32_t ind = 0; ind < dev_count; ind++) {
        std::string dev_name = "RPC" + std::to_string(dev_id);
        std::string dev_desc = std::string(endpoint);
```

```cpp
        dev_id++;
    }
    ggml_backend_reg_t reg = new ggml_backend_reg {
        /* .api_version = */ GGML_BACKEND_API_VERSION,
        /* .iface       = */ ggml_backend_rpc_reg_interface,
        /* .context     = */ ctx
    };
    reg_map[endpoint] = reg;
    return reg;
```

```cpp
        /* .context     = */ ctx
    };
    reg_map[endpoint] = reg;
    return reg;
}
```

```cpp
    };
    reg_map[endpoint] = reg;
    return reg;
}

```

Verdict (recorded in `defect_review.json`):

- `verdict`: `REVIEWED`
- `is_true_positive`: `false` — **false positive**
- `failure_reason`: `ownership`
- `needs_dfg`: `false`
- `missing_capability`: `对象图传递所有权（transitive ownership via object graph）`
- `confidence`: `0.85`
- `reason`: `` ggml/src/ggml-rpc/ggml-rpc.cpp:2348 `dev_ctx = new …{}` 随后装进 dev->context、dev push 进 ctx->devices、reg 经返回值逃逸并存入 reg_map——dev_ctx 的所有权随对象图整体移交；「存进堆对象成员再随根逃逸」的传递路径图不建模（只认 RETURN 直接逃逸，故 reg 的逃逸判定没有帮到 dev_ctx）。 ``
- `annotator`: `AI 提议 + 人工确认（Claude Code 提议 / Kyber5323 逐条确认，2026-09-24）`
- `notes`: `null`

### DFV1-redis-50-dcf8abc68a1eb2df

- type: `4.2` / `double_free`
- status: `resolved` (confidence 0.5)
- subject: `cmd` in `main`
- candidate: `E-DEFECT-8f6a6e893acbaa2ad564abad`
- anchor: `event:69b9045db290c8f1ca5685cdafd4e2a66c26e317456440ddbfe9bea48ef93079`
- source: `src/redis-benchmark.c:1939-1939`, `src/redis-benchmark.c:1986-1986`

Facts:

- `event`='RELEASE', `matched_name`='free', `method`='main', `point`='event:ba4d446d2a8d875b6c623b7e23999ae38f1da8cd0314ec68c18227b00f2cae4e', `source`='src/redis-benchmark.c:1939-1939', `subject`='cmd', `subject_from`='argument'
- `event`='RELEASE', `matched_name`='free', `method`='main', `point`='event:69b9045db290c8f1ca5685cdafd4e2a66c26e317456440ddbfe9bea48ef93079', `source`='src/redis-benchmark.c:1986-1986', `subject`='cmd', `subject_from`='argument'

Uncertain facts:


Missing evidence:

- the repository's release convention: whether a release leaves the pointer dangling or nulls it, which decides whether the second release is a double free or a legal `free(NULL)`
- whether two names really address one object: the alias annotation is a same-statement copy fact, so a pair joined through it is a candidate and not an identity

```c
            len = redisFormatCommand(&cmd,"RPUSH mylist%s %s",tag,data);
            benchmark("RPUSH",cmd,len);
            free(cmd);
        }

```

```c
            len = redisFormatCommand(&cmd,"ZPOPMIN myzset%s",tag);
            benchmark("ZPOPMIN",cmd,len);
            free(cmd);
        }

```

Verdict (recorded in `defect_review.json`):

- `verdict`: `REVIEWED`
- `is_true_positive`: `false` — **false positive**
- `failure_reason`: `contract`
- `needs_dfg`: `true`
- `missing_capability`: `被调经出参的 def（callee out-param write）`
- `confidence`: `0.85`
- `reason`: `` src/redis-benchmark.c:1939 与 :1986 的两个 free 属于不同 benchmark 块（RPUSH / ZPOPMIN 等互不相干的两段测试代码），块间 `cmd = redisFormatCommand(&cmd, …)` 重新分配——杀死第二次 free 的 intervening def 是被调经出参 `&cmd` 写入的，图看不到被调帧内的分配。补上跨方法出参 def 边即淘汰。 ``
- `annotator`: `AI 提议 + 人工确认（Claude Code 提议 / Kyber5323 逐条确认，2026-09-24）`
- `notes`: `null`

### DFV1-llama.cpp-69-bbcc905b1bc1a8c6

- type: `4.1` / `resource_lifetime`
- status: `unresolved` (confidence 0.2)
- subject: `` in `console.readline_simple`
- candidate: `E-DEFECT-5b6511e78f24f9fb5a9ad38d`
- anchor: `event:dfc9856d29244d3231e9a41fdaacf8bf6154db573ac1a4c803fc11b737ead0f8`
- source: `common/console.cpp:1060-1060`, `common/console.cpp:1062-1062`, `common/console.cpp:1063-1063`, `common/console.cpp:1066-1066`, `common/console.cpp:1080-1080`, `common/console.cpp:1067-1067`, `common/console.cpp:1068-1068`, `common/console.cpp:1072-1072`, `common/console.cpp:1069-1069`, `common/console.cpp:1070-1070`, `common/console.cpp:1073-1073`

Facts:

- `event`='ALLOC', `matched_name`='getline', `method`='console.readline_simple', `point`='event:dfc9856d29244d3231e9a41fdaacf8bf6154db573ac1a4c803fc11b737ead0f8', `source`='common/console.cpp:1060-1060', `subject`='', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='console.readline_simple', `point`='event:7cc55c9f31b6de66f3018a950ad4860300843690a91a77d8438a2246c8aaa1bd', `source`='common/console.cpp:1060-1060', `subject`='', `subject_from`=''
- `event`='CHECK', `matched_name`='!std::getline(std::cin,line)', `method`='console.readline_simple', `point`='event:223f91c016aeae8bd36ef78883eaf78a70f3b209903b5b9e8b97dee38b71cc00', `source`='common/console.cpp:1060-1060', `subject`='', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='console.readline_simple', `point`='event:32fa21c3d117fbd1b5881fec37bc5a5b5a8fdf7962e21b81ad7b74ade4dd9bf2', `source`='common/console.cpp:1062-1062', `subject`='line', `subject_from`='receiver'
- `event`='RETURN', `matched_name`='return_statement', `method`='console.readline_simple', `point`='event:f3148bcd00b74d2934815b505c7aa0b35df14eae24efc29cf01c171c5e9e7275', `source`='common/console.cpp:1063-1063', `subject`='', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='console.readline_simple', `point`='event:c8eeb59b27087fcee23df5bf94c58ab9bc717b2021f8ff6d7bce41361c3730f6', `source`='common/console.cpp:1066-1066', `subject`='line', `subject_from`='receiver'
- `event`='CHECK', `matched_name`='!line.empty()', `method`='console.readline_simple', `point`='event:4981667bd9b3d5e915321bb52db63d72180e5dd036e31ffe1174826e035efb60', `source`='common/console.cpp:1066-1066', `subject`='', `subject_from`=''
- `event`='RETURN', `matched_name`='return_statement', `method`='console.readline_simple', `point`='event:80c6e8500cf962a34f2fcc4f02d48662dbea4ee8080b9ee1925f9409a08ca1fa', `source`='common/console.cpp:1080-1080', `subject`='multiline_input', `subject_from`='return'
- `event`='CALL', `matched_name`='call_expression', `method`='console.readline_simple', `point`='event:d435dbae0f7a6872af413713e2b96e7aaa41d786a19b53b2d72c1e25258b529e', `source`='common/console.cpp:1067-1067', `subject`='last', `subject_from`='assignment'
- `event`='BRANCH', `matched_name`="last=='/'", `method`='console.readline_simple', `point`='event:743d5a27c6afc7f4d65b72223d34a8a337a678fec40831b3300f1f9b07b96513', `source`='common/console.cpp:1068-1068', `subject`='', `subject_from`=''
- `event`='BRANCH', `matched_name`="last=='\\\\'", `method`='console.readline_simple', `point`='event:f0dfc24c83072936e0198ac7c5dd3bbd833fa9a1e498232e451bfeb604718c81', `source`='common/console.cpp:1072-1072', `subject`='', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='console.readline_simple', `point`='event:f9339d7eac69bf1cf1e9d72d307e0663d313ce6e3dd0975e24985b57e6ae924f', `source`='common/console.cpp:1069-1069', `subject`='line', `subject_from`='receiver'
- `event`='RETURN', `matched_name`='return_statement', `method`='console.readline_simple', `point`='event:ae1af3aea25d24489385fc76d8bd3fc68696df40788bcef9b31ce9621238d40c', `source`='common/console.cpp:1070-1070', `subject`='', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='console.readline_simple', `point`='event:4ef6b0bdc630e823cd4b13be81236644699a610bf83914c5bbb0261703b539df', `source`='common/console.cpp:1073-1073', `subject`='line', `subject_from`='receiver'

Uncertain facts:

- `decisive`=True, `detail`='the acquisition names no target, so the candidate cannot be attributed', `fact`='SUBJECT_UNRESOLVED', `status`='ambiguous'

Missing evidence:

- the ownership contract of a callee: whether a function the resource is passed to takes ownership, borrows it, or frees it
- RAII and smart pointers: `unique_ptr<T> p(new T)` is an ALLOC with no RELEASE in the graph, so every owning smart pointer reads as a leak
- aliasing and reassignment: `q = p; free(q)`, `free(p); p = NULL` and `p = realloc(p, n)` are all invisible without a data-flow graph
- a release performed inside a callee: the graph only sees RELEASE points in the method it walked

```cpp
        WideCharToMultiByte(CP_UTF8, 0, &wline[0], (int)wline.size(), &line[0], size_needed, NULL, NULL);
#else
        if (!std::getline(std::cin, line)) {
            // Input stream is bad or EOF received
            line.clear();
```

```cpp
        if (!std::getline(std::cin, line)) {
            // Input stream is bad or EOF received
            line.clear();
            return false;
        }
```

```cpp
            // Input stream is bad or EOF received
            line.clear();
            return false;
        }
#endif
```

```cpp
        }
#endif
        if (!line.empty()) {
            char last = line.back();
            if (last == '/') { // Always return control on '/' symbol
```

```cpp

        // By default, continue input if multiline_input is set
        return multiline_input;
    }

```

```cpp
#endif
        if (!line.empty()) {
            char last = line.back();
            if (last == '/') { // Always return control on '/' symbol
                line.pop_back();
```

```cpp
        if (!line.empty()) {
            char last = line.back();
            if (last == '/') { // Always return control on '/' symbol
                line.pop_back();
                return false;
```

```cpp
                return false;
            }
            if (last == '\\') { // '\\' changes the default action
                line.pop_back();
                multiline_input = !multiline_input;
```

```cpp
            char last = line.back();
            if (last == '/') { // Always return control on '/' symbol
                line.pop_back();
                return false;
            }
```

```cpp
            if (last == '/') { // Always return control on '/' symbol
                line.pop_back();
                return false;
            }
            if (last == '\\') { // '\\' changes the default action
```

```cpp
            }
            if (last == '\\') { // '\\' changes the default action
                line.pop_back();
                multiline_input = !multiline_input;
            }
```

Verdict (recorded in `defect_review.json`):

- `verdict`: `REVIEWED`
- `is_true_positive`: `false` — **false positive**
- `failure_reason`: `extractor`
- `needs_dfg`: `false`
- `missing_capability`: `getline 误入 ALLOC 词表（std::getline misclassified）`
- `confidence`: `0.9`
- `reason`: `` common/console.cpp:1060 的 ALLOC matched_name=getline 且主体为空（SUBJECT_UNRESOLVED）——`std::getline(std::cin, line)` 是读流不是分配；与 case 10 同一提取词表错误。 ``
- `annotator`: `AI 提议 + 人工确认（Claude Code 提议 / Kyber5323 逐条确认，2026-09-24）`
- `notes`: `null`

### DFV1-redis-50-8b60100a4fc409d1

- type: `4.3` / `use_after_free`
- status: `ambiguous` (confidence 0.3)
- subject: `len` in `main`
- candidate: `E-DEFECT-a39a8aa7b5b3d990d30692e9`
- anchor: `event:090f09279dce08ea3e2709fd6197fa04682442f148406511e7cf809094e55236`
- source: `src/redis-benchmark.c:1997-1997`, `src/redis-benchmark.c:2032-2032`

Facts:

- `event`='RELEASE', `matched_name`='free', `method`='main', `point`='event:43128c3ea24180ea738c78b268e3b9da91f4255b3a6fb9328a7d17b3bc5be87d', `source`='src/redis-benchmark.c:1997-1997', `subject`='cmd', `subject_from`='argument'
- `event`='CALL', `matched_name`='call_expression', `method`='main', `point`='event:090f09279dce08ea3e2709fd6197fa04682442f148406511e7cf809094e55236', `source`='src/redis-benchmark.c:2032-2032', `subject`='len', `subject_from`='assignment'

Uncertain facts:

- `decisive`=True, `detail`='the use is a call argument, and the graph does not distinguish passing the pointer (`f(p)`) from passing its address (`f(&p)`)', `fact`='USE_KIND_UNKNOWN', `point`='event:090f09279dce08ea3e2709fd6197fa04682442f148406511e7cf809094e55236', `source`='src/redis-benchmark.c:2032-2032', `status`='ambiguous'

Missing evidence:

- whether the use reads the pointer value or the pointee: `if (p)` after a release compares a dangling value, and the graph does not separate the two readings
- what a release wrapper does: whether it nulls the caller's pointer, whether it releases the whole object, and whether it releases at all
- aliasing and reassignment: `q = p; free(q)`, `free(p); p = NULL` and `p = realloc(p, n)` are all invisible without a data-flow graph

```c
            len = redisFormatCommand(&cmd,"LPUSH mylist%s %s",tag,data);
            benchmark("LPUSH (needed to benchmark LRANGE)",cmd,len);
            free(cmd);
        }

```

```c
                cmd_argv[i+1] = data;
            }
            len = redisFormatCommandArgv(&cmd,21,cmd_argv,NULL);
            benchmark("MSET (10 keys)",cmd,len);
            free(cmd);
```

Verdict (recorded in `defect_review.json`):

- `verdict`: `REVIEWED`
- `is_true_positive`: `false` — **false positive**
- `failure_reason`: `missing_read_write`
- `needs_dfg`: `false`
- `missing_capability`: `取址实参 vs 值实参（address-of vs value argument）`
- `confidence`: `0.8`
- `reason`: `` src/redis-benchmark.c:1997 的 free 与 :2032 的「使用」之间隔着多个 benchmark 块，且 :2032 `len = redisFormatCommandArgv(&cmd, …)` 传的是 &cmd（变量的地址），不是悬垂指针值——图不区分 f(p) 与 f(&p)（USE_KIND_UNKNOWN 注解自述），且其间 cmd 已被重新分配。 ``
- `annotator`: `AI 提议 + 人工确认（Claude Code 提议 / Kyber5323 逐条确认，2026-09-24）`
- `notes`: `null`

### DFV1-llama.cpp-69-8700674b9ba8bea4

- type: `4.2` / `double_free`
- status: `resolved` (confidence 0.5)
- subject: `rmt_name` in `apir_initialize`
- candidate: `E-DEFECT-d9ab17c13b452c0945d53fe1`
- anchor: `event:fd671194cd8e055ce051795e002c513c6e79af1560fd9031d751006277508414`
- source: `ggml/src/ggml-virtgpu/ggml-backend-reg.cpp:72-72`, `ggml/src/ggml-virtgpu/ggml-backend-reg.cpp:76-76`

Facts:

- `event`='RELEASE', `matched_name`='free', `method`='apir_initialize', `point`='event:e5780257d4a128c4bdba8890cfca958ff8f47748b2bb7e71ea7a38f1e6b889f4', `source`='ggml/src/ggml-virtgpu/ggml-backend-reg.cpp:72-72', `subject`='rmt_name', `subject_from`='argument'
- `event`='RELEASE', `matched_name`='free', `method`='apir_initialize', `point`='event:fd671194cd8e055ce051795e002c513c6e79af1560fd9031d751006277508414', `source`='ggml/src/ggml-virtgpu/ggml-backend-reg.cpp:76-76', `subject`='rmt_name', `subject_from`='argument'

Uncertain facts:


Missing evidence:

- the repository's release convention: whether a release leaves the pointer dangling or nulls it, which decides whether the second release is a double free or a legal `free(NULL)`
- whether two names really address one object: the alias annotation is a same-statement copy fact, so a pair joined through it is a candidate and not an identity

```cpp
            gpu->cached_buffer_type.name = (char *) malloc(prefixed_len);
            if (!gpu->cached_buffer_type.name) {
                free(rmt_name);
                GGML_ABORT(GGML_VIRTGPU "%s: failed to allocate memory for prefixed buffer type name", __func__);
            }
```

```cpp
            }
            snprintf(gpu->cached_buffer_type.name, prefixed_len, "[virtgpu] %s", rmt_name);
            free(rmt_name);
        }

```

Verdict (recorded in `defect_review.json`):

- `verdict`: `REVIEWED`
- `is_true_positive`: `false` — **false positive**
- `failure_reason`: `contract`
- `needs_dfg`: `false`
- `missing_capability`: `noreturn 语义（GGML_ABORT / abort）`
- `confidence`: `0.85`
- `reason`: `` ggml/src/ggml-virtgpu/ggml-backend-reg.cpp:72 的 free 在 malloc 失败臂内，臂尾 `GGML_ABORT(...)` 终止进程；:76 的 free 在 snprintf 成功路径——两条臂互斥，R1→R2 不可达。图未建模 GGML_ABORT 的 noreturn 语义，失败臂被当成可落穿。 ``
- `annotator`: `AI 提议 + 人工确认（Claude Code 提议 / Kyber5323 逐条确认，2026-09-24）`
- `notes`: `null`

### DFV1-redis-50-4141c43e3a33db4a

- type: `4.6` / `resource_lifetime`
- status: `ambiguous` (confidence 0.4)
- subject: `config` in `freeClient`
- candidate: `E-DEFECT-084f441d3c254662c466891a`
- anchor: `event:85191020f62416597c149f074ff0518298c63df6f6b60e1ca0c50c2736c7be8a`
- source: `src/redis-benchmark.c:350-350`, `src/redis-benchmark.c:352-352`, `src/redis-benchmark.c:353-353`, `src/redis-benchmark.c:354-354`, `src/redis-benchmark.c:355-355`

Facts:

- `event`='LOCK', `matched_name`='pthread_mutex_lock', `method`='freeClient', `point`='event:85191020f62416597c149f074ff0518298c63df6f6b60e1ca0c50c2736c7be8a', `source`='src/redis-benchmark.c:350-350', `subject`='config', `subject_from`='argument'
- `event`='CALL', `matched_name`='call_expression', `method`='freeClient', `point`='event:4f1d2c0c8657beac9a209e362e2af0b29aa31817cb708597df627b037b6d2aac', `source`='src/redis-benchmark.c:352-352', `subject`='ln', `subject_from`='assignment'
- `event`='CALL', `matched_name`='call_expression', `method`='freeClient', `point`='event:2e04fc106a13639dbbb1cd0d3b237317591d9b8787b7c52d365c2d9d5ad45d1b', `source`='src/redis-benchmark.c:353-353', `subject`='', `subject_from`=''
- `event`='THROW', `matched_name`='assert', `method`='freeClient', `point`='event:a5866c3627394499fdc6c3d96e03e8d2825ba44c1b1bfca8d48de67575014888', `source`='src/redis-benchmark.c:353-353', `subject`='', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='freeClient', `point`='event:3bcc45c68015679704e1d54358a8b6b9d49cb3720ef5e8db566e423954bcc913', `source`='src/redis-benchmark.c:354-354', `subject`='config', `subject_from`='argument'
- `event`='BRANCH', `matched_name`='config.num_threads', `method`='freeClient', `point`='event:023819a962763991c7829389d3445f8e279d39a55c5a20528ca25d1821684e09', `source`='src/redis-benchmark.c:355-355', `subject`='', `subject_from`=''

Uncertain facts:

- `decisive`=True, `detail`='the name is the root of a longer expression (`s` in `s->buf`), so the same name elsewhere may be a different object', `fact`='SUBJECT_PATH', `point`='event:85191020f62416597c149f074ff0518298c63df6f6b60e1ca0c50c2736c7be8a', `source`='src/redis-benchmark.c:350-350', `status`='ambiguous'

Missing evidence:

- the ownership contract of a callee: whether a function the resource is passed to takes ownership, borrows it, or frees it
- RAII and smart pointers: `unique_ptr<T> p(new T)` is an ALLOC with no RELEASE in the graph, so every owning smart pointer reads as a leak
- aliasing and reassignment: `q = p; free(q)`, `free(p); p = NULL` and `p = realloc(p, n)` are all invisible without a data-flow graph
- a release performed inside a callee: the graph only sees RELEASE points in the method it walked
- whether the error path is reachable in practice: the graph reports the path exists, not that the input reaches it
- whether a destructor or another path releases the lock first
- the guard-to-mutex binding: `std::unique_lock<T> g(m)` releases through `g`, and `g.unlock()` is an operation on the guard, not on `m`, so a walk that joins on the mutex's identity cannot see the release

```c
    zfree(c->stagptr);
    zfree(c);
    if (config.num_threads) pthread_mutex_lock(&(config.liveclients_mutex));
    config.liveclients--;
    ln = listSearchKey(config.clients,c);
```

```c
    if (config.num_threads) pthread_mutex_lock(&(config.liveclients_mutex));
    config.liveclients--;
    ln = listSearchKey(config.clients,c);
    assert(ln != NULL);
    listDelNode(config.clients,ln);
```

```c
    config.liveclients--;
    ln = listSearchKey(config.clients,c);
    assert(ln != NULL);
    listDelNode(config.clients,ln);
    if (config.num_threads) pthread_mutex_unlock(&(config.liveclients_mutex));
```

```c
    ln = listSearchKey(config.clients,c);
    assert(ln != NULL);
    listDelNode(config.clients,ln);
    if (config.num_threads) pthread_mutex_unlock(&(config.liveclients_mutex));
}
```

```c
    assert(ln != NULL);
    listDelNode(config.clients,ln);
    if (config.num_threads) pthread_mutex_unlock(&(config.liveclients_mutex));
}

```

Verdict (recorded in `defect_review.json`):

- `verdict`: `REVIEWED`
- `is_true_positive`: `false` — **false positive**
- `failure_reason`: `contract`
- `needs_dfg`: `false`
- `missing_capability`: `noreturn 语义（assert / abort）`
- `confidence`: `0.75`
- `reason`: `` src/redis-benchmark.c:350 的 LOCK 与 :354 的 UNLOCK 共用同一 `if (config.num_threads)` 守卫——真实路径上锁定/释放成对；唯一「持锁退出」的路径是 :352 `assert(ln != NULL)` 失败 abort，进程即终止。assert 的 noreturn 语义缺失使错误臂被当成可达出口。 ``
- `annotator`: `AI 提议 + 人工确认（Claude Code 提议 / Kyber5323 逐条确认，2026-09-24）`
- `notes`: `null`

### DFV1-llama.cpp-69-c8df5f3a2d7feaf3

- type: `4.2` / `double_free`
- status: `unresolved` (confidence 0.1)
- subject: `e` in `ggml_backend_opencl_buffer_context.ggml_backend_opencl_buffer_context`
- candidate: `E-DEFECT-6f6b677693d91c77ae9a44ef`
- anchor: `event:01a673022c3a653af0f306239cfc0fbe789fa0032c3cfff52d53b68e5cd0c444`
- source: `ggml/src/ggml-opencl/ggml-opencl.cpp:9295-9295`, `ggml/src/ggml-opencl/ggml-opencl.cpp:9313-9313`

Facts:

- `event`='RELEASE', `matched_name`='delete_expression', `method`='ggml_backend_opencl_buffer_context.ggml_backend_opencl_buffer_context', `point`='event:2d0d575dc97eb3cbea6e374854224f1a1eed151424d1bb5cecd8c1c766217e0a', `source`='ggml/src/ggml-opencl/ggml-opencl.cpp:9295-9295', `subject`='e', `subject_from`='argument'
- `event`='RELEASE', `matched_name`='delete_expression', `method`='ggml_backend_opencl_buffer_context.ggml_backend_opencl_buffer_context', `point`='event:01a673022c3a653af0f306239cfc0fbe789fa0032c3cfff52d53b68e5cd0c444', `source`='ggml/src/ggml-opencl/ggml-opencl.cpp:9313-9313', `subject`='e', `subject_from`='argument'

Uncertain facts:

- `decisive`=True, `detail`='a release on this path names no declaration, so the two releases are joined by spelling rather than by object', `fact`='IDENTITY_UNKNOWN', `point`='event:01a673022c3a653af0f306239cfc0fbe789fa0032c3cfff52d53b68e5cd0c444', `source`='ggml/src/ggml-opencl/ggml-opencl.cpp:9313-9313', `status`='ambiguous'

Missing evidence:

- the repository's release convention: whether a release leaves the pointer dangling or nulls it, which decides whether the second release is a double free or a legal `free(NULL)`
- whether two names really address one object: the alias annotation is a same-statement copy fact, so a pair joined through it is a candidate and not an identity

```cpp
        }
        for (ggml_tensor_extra_cl_iq4_nl * e : temp_tensor_extras_iq4_nl_in_use) {
            delete e;
        }
        for (ggml_tensor_extra_cl_q4_K * e : temp_tensor_extras_q4_K) {
```

```cpp
        }
        for (ggml_tensor_extra_cl_q5_K * e : temp_tensor_extras_q5_K_in_use) {
            delete e;
        }
    }
```

Verdict (recorded in `defect_review.json`):

- `verdict`: `REVIEWED`
- `is_true_positive`: `false` — **false positive**
- `failure_reason`: `identity`
- `needs_dfg`: `false`
- `missing_capability`: `循环变量/局部作用域身份（for-scope identity）`
- `confidence`: `0.9`
- `reason`: `` ggml/src/ggml-opencl/ggml-opencl.cpp:9295 `for (e : temp_tensor_extras_iq4_nl_in_use) delete e;` 与 :9313 `for (e : temp_tensor_extras_q5_K_in_use) delete e;` 是两个不同容器的 range-for 循环变量，共用拼写 e——循环变量无作用域身份（IDENTITY_UNKNOWN，5B 登记缺口），按拼写连接成同一身份。 ``
- `annotator`: `AI 提议 + 人工确认（Claude Code 提议 / Kyber5323 逐条确认，2026-09-24）`
- `notes`: `null`

### DFV1-redis-50-d85693b7ea77dfb9

- type: `4.6` / `resource_lifetime`
- status: `resolved` (confidence 0.6)
- subject: `moduleGIL` in `moduleInitModulesSystem`
- candidate: `E-DEFECT-44aa907661afea9795696458`
- anchor: `event:69c572870871e077808af9a67e61aee9a96b030d37651d6981c4840326cbef15`
- source: `src/module.c:13423-13423`

Facts:

- `event`='LOCK', `matched_name`='pthread_mutex_lock', `method`='moduleInitModulesSystem', `point`='event:69c572870871e077808af9a67e61aee9a96b030d37651d6981c4840326cbef15', `source`='src/module.c:13423-13423', `subject`='moduleGIL', `subject_from`='argument'

Uncertain facts:


Missing evidence:

- the ownership contract of a callee: whether a function the resource is passed to takes ownership, borrows it, or frees it
- RAII and smart pointers: `unique_ptr<T> p(new T)` is an ALLOC with no RELEASE in the graph, so every owning smart pointer reads as a leak
- aliasing and reassignment: `q = p; free(q)`, `free(p); p = NULL` and `p = realloc(p, n)` are all invisible without a data-flow graph
- a release performed inside a callee: the graph only sees RELEASE points in the method it walked
- whether the error path is reachable in practice: the graph reports the path exists, not that the input reaches it
- whether a destructor or another path releases the lock first
- the guard-to-mutex binding: `std::unique_lock<T> g(m)` releases through `g`, and `g.unlock()` is an operation on the guard, not on `m`, so a walk that joins on the mutex's identity cannot see the release

```c
    /* Our thread-safe contexts GIL must start with already locked:
     * it is just unlocked when it's safe. */
    pthread_mutex_lock(&moduleGIL);
}

```

Verdict (recorded in `defect_review.json`):

- `verdict`: `REVIEWED`
- `is_true_positive`: `false` — **false positive**
- `failure_reason`: `contract`
- `needs_dfg`: `false`
- `missing_capability`: `跨帧配对释放（paired unlock in another frame）`
- `confidence`: `0.9`
- `reason`: `src/module.c:13423 加锁后函数即返回，注释原文 Our thread-safe contexts GIL must start with already locked——moduleInitModulesSystem 按设计持锁返回，释放由 moduleReleaseGIL 承担；这是文档化的协议不变量，不是泄漏。`
- `annotator`: `AI 提议 + 人工确认（Claude Code 提议 / Kyber5323 逐条确认，2026-09-24）`
- `notes`: `null`

### DFV1-llama.cpp-69-ca753350c046dc65

- type: `4.3` / `use_after_free`
- status: `ambiguous` (confidence 0.3)
- subject: `e` in `ggml_backend_opencl_buffer_context.ggml_backend_opencl_buffer_context`
- candidate: `E-DEFECT-22f7b8f4de6d0cbafcade67b`
- anchor: `event:772cdf3e617883524b6216c0294d183de6d94a38d2ddfcb51d25488c8d638377`
- source: `ggml/src/ggml-opencl/ggml-opencl.cpp:9256-9256`, `ggml/src/ggml-opencl/ggml-opencl.cpp:9259-9259`

Facts:

- `event`='RELEASE', `matched_name`='delete_expression', `method`='ggml_backend_opencl_buffer_context.ggml_backend_opencl_buffer_context', `point`='event:df545f3fd8f70c4821d7c18d0a144f2ea0b78ef8c2a1c82124139ed9740a11be', `source`='ggml/src/ggml-opencl/ggml-opencl.cpp:9256-9256', `subject`='e', `subject_from`='argument'
- `event`='READ', `matched_name`='e', `method`='ggml_backend_opencl_buffer_context.ggml_backend_opencl_buffer_context', `point`='event:772cdf3e617883524b6216c0294d183de6d94a38d2ddfcb51d25488c8d638377', `source`='ggml/src/ggml-opencl/ggml-opencl.cpp:9259-9259', `subject`='e', `subject_from`=''

Uncertain facts:

- `decisive`=True, `detail`='no declaration of this name in this file, so the identity falls back to the spelling -- a macro, an enum constant, or a global declared in a header the parser does not read', `fact`='IDENTITY_UNKNOWN', `point`='event:772cdf3e617883524b6216c0294d183de6d94a38d2ddfcb51d25488c8d638377', `source`='ggml/src/ggml-opencl/ggml-opencl.cpp:9259-9259', `status`='ambiguous'

Missing evidence:

- whether the use reads the pointer value or the pointee: `if (p)` after a release compares a dangling value, and the graph does not separate the two readings
- what a release wrapper does: whether it nulls the caller's pointer, whether it releases the whole object, and whether it releases at all
- aliasing and reassignment: `q = p; free(q)`, `free(p); p = NULL` and `p = realloc(p, n)` are all invisible without a data-flow graph

```cpp
        }
        for (ggml_tensor_extra_cl_q4_1 * e : temp_tensor_extras_q4_1) {
            delete e;
        }
        for (ggml_tensor_extra_cl_q4_1 * e : temp_tensor_extras_q4_1_in_use) {
```

```cpp
        }
        for (ggml_tensor_extra_cl_q4_1 * e : temp_tensor_extras_q4_1_in_use) {
            delete e;
        }
        for (ggml_tensor_extra_cl_q5_0 * e : temp_tensor_extras_q5_0) {
```

Verdict (recorded in `defect_review.json`):

- `verdict`: `REVIEWED`
- `is_true_positive`: `false` — **false positive**
- `failure_reason`: `identity`
- `needs_dfg`: `false`
- `missing_capability`: `循环变量/局部作用域身份（for-scope identity）`
- `confidence`: `0.9`
- `reason`: `` ggml/src/ggml-opencl/ggml-opencl.cpp:9256 `delete e`（temp_tensor_extras_q4_1_in_use 循环）与 :9259 的 e 是相邻两个 range-for 的循环变量（不同容器、不同对象），拼写同为 e——与 case 18 同一作用域身份缺口。 ``
- `annotator`: `AI 提议 + 人工确认（Claude Code 提议 / Kyber5323 逐条确认，2026-09-24）`
- `notes`: `null`

### DFV1-redis-50-2ad47121f22a062b

- type: `9.1` / `error_handling`
- status: `ambiguous` (confidence 0.2)
- subject: `asmManager` in `asmInit`
- candidate: `E-DEFECT-70aa1250fee907f5188e9e99`
- anchor: `event:d7f36652f3a80f9fb470189039229211856d0ee9777d795d549f1cc30612ee87`
- source: `src/cluster_asm.c:212-212`

Facts:

- `event`='CALL', `matched_name`='call_expression', `method`='asmInit', `point`='event:d7f36652f3a80f9fb470189039229211856d0ee9777d795d549f1cc30612ee87', `source`='src/cluster_asm.c:212-212', `subject`='asmManager', `subject_from`='assignment'

Uncertain facts:

- `decisive`=True, `detail`='the name is the root of a longer expression (`s` in `s->buf`), so the same name elsewhere may be a different object', `fact`='SUBJECT_PATH', `point`='event:d7f36652f3a80f9fb470189039229211856d0ee9777d795d549f1cc30612ee87', `source`='src/cluster_asm.c:212-212', `status`='ambiguous'

Missing evidence:

- which APIs must have their result checked: the graph sees a call whose result was stored and not examined, and whether this call has a failure mode worth checking is API semantics the AI side holds
- whether a result was discarded outright: a CALL point records where a result went, never that it went nowhere, so `f();` as a bare statement is outside this query entirely -- and so is a result passed straight on as an argument, which no branch examines and no variable holds

```c
void asmInit(void) {
    asmManager = zcalloc(sizeof(*asmManager));
    asmManager->tasks = listCreate();
    asmManager->archived_tasks = listCreate();
    asmManager->pending_trim_jobs = listCreate();
```

Verdict (recorded in `defect_review.json`):

- `verdict`: `REVIEWED`
- `is_true_positive`: `false` — **false positive**
- `failure_reason`: `contract`
- `needs_dfg`: `false`
- `missing_capability`: `MUST_CHECK API 语义（AI-side）`
- `confidence`: `0.7`
- `reason`: `` src/cluster_asm.c:212 `asmManager->tasks = listCreate();`——listCreate 内部走 zmalloc（OOM 即 panic），实际上不会返回 NULL；「不检查」是 redis 全仓惯例而非缺陷。9.1 已登记「哪些 API 必须检查结果」是 AI 侧语义，本条是该登记的代表样本。 ``
- `annotator`: `AI 提议 + 人工确认（Claude Code 提议 / Kyber5323 逐条确认，2026-09-24）`
- `notes`: `null`

### DFV1-llama.cpp-69-c0914d10fdccce31

- type: `4.3` / `use_after_free`
- status: `resolved` (confidence 0.5)
- subject: `dst` in `llama_sampler_copy`
- candidate: `E-DEFECT-b18b32e1b9c194d6c12cda33`
- anchor: `event:283784976ab4de31532d48be7339224d002f938d0569cd1df6d5af3053da9d32`
- source: `src/llama-sampler.cpp:4316-4316`, `src/llama-sampler.cpp:4320-4320`

Facts:

- `event`='RELEASE', `matched_name`='free', `method`='llama_sampler_copy', `point`='event:05ead4207caec4276894f0cc30f8be768f660c1b3d29912c8554ce08b06ed296', `source`='src/llama-sampler.cpp:4316-4316', `subject`='dst', `subject_from`='argument'
- `event`='DEREFERENCE', `matched_name`='dst->ctx', `method`='llama_sampler_copy', `point`='event:283784976ab4de31532d48be7339224d002f938d0569cd1df6d5af3053da9d32', `source`='src/llama-sampler.cpp:4320-4320', `subject`='dst', `subject_from`=''

Uncertain facts:


Missing evidence:

- whether the use reads the pointer value or the pointee: `if (p)` after a release compares a dangling value, and the graph does not separate the two readings
- what a release wrapper does: whether it nulls the caller's pointer, whether it releases the whole object, and whether it releases at all
- aliasing and reassignment: `q = p; free(q)`, `free(p); p = NULL` and `p = realloc(p, n)` are all invisible without a data-flow graph

```cpp
    // free dst's old state (frees dst->ctx, including children for a chain)
    if (dst->iface->free) {
        dst->iface->free(dst);
    }

```

```cpp

    // transplant tmp's state into dst, then destroy the (now empty) temp shell
    dst->ctx = tmp->ctx;
    tmp->ctx = nullptr;
    delete tmp;
```

Verdict (recorded in `defect_review.json`):

- `verdict`: `REVIEWED`
- `is_true_positive`: `false` — **false positive**
- `failure_reason`: `contract`
- `needs_dfg`: `false`
- `missing_capability`: `释放器方法的实际释放粒度（iface->free granularity）`
- `confidence`: `0.8`
- `reason`: `` src/llama-sampler.cpp:4316 `dst->iface->free(dst)` 释放的是 dst 的内部状态（注释明写 free the dst state, including children for a chain，即 dst->ctx），不是 dst 壳对象；:4320 `dst->ctx = tmp->ctx` 移植新状态。RELEASE 事件按实参根 dst 记身份，实际释放对象是成员状态——与后续 dst->ctx 写点不构成 UAF。 ``
- `annotator`: `AI 提议 + 人工确认（Claude Code 提议 / Kyber5323 逐条确认，2026-09-24）`
- `notes`: `null`

### DFV1-redis-50-0b7b42aaae323b80

- type: `9.1` / `error_handling`
- status: `resolved` (confidence 0.4)
- subject: `d` in `arSparsePromote`
- candidate: `E-DEFECT-bdfcfefe0f148d4c8ef0a272`
- anchor: `event:8ad04014d60d47ea71ae62fdebb302215a6b704a30308dfccb3230a2e0c1fdf8`
- source: `src/sparsearray.c:432-432`, `src/sparsearray.c:433-433`

Facts:

- `event`='CALL', `matched_name`='call_expression', `method`='arSparsePromote', `point`='event:8ad04014d60d47ea71ae62fdebb302215a6b704a30308dfccb3230a2e0c1fdf8', `source`='src/sparsearray.c:432-432', `subject`='d', `subject_from`='assignment'
- `event`='DEREFERENCE', `matched_name`='d->encoding', `method`='arSparsePromote', `point`='event:6700d915d84716f192409d87c935f9a2cf164a5889cc47b2e2e3a02b77b5f2eb', `source`='src/sparsearray.c:433-433', `subject`='d', `subject_from`=''

Uncertain facts:


Missing evidence:

- which APIs must have their result checked: the graph sees a call whose result was stored and not examined, and whether this call has a failure mode worth checking is API semantics the AI side holds
- whether a result was discarded outright: a CALL point records where a result went, never that it went nowhere, so `f();` as a bare statement is outside this query entirely -- and so is a result passed straight on as an argument, which no branch examines and no variable holds

```c
    }

    arSlice *d = arAllocAndTrack(ar, arDenseAllocSize(winsize));
    d->encoding = AR_SLICE_DENSE;
    d->count = s->count;
```

```c

    arSlice *d = arAllocAndTrack(ar, arDenseAllocSize(winsize));
    d->encoding = AR_SLICE_DENSE;
    d->count = s->count;
    d->layout.dense.offset = offset;
```

Verdict (recorded in `defect_review.json`):

- `verdict`: `REVIEWED`
- `is_true_positive`: `false` — **false positive**
- `failure_reason`: `contract`
- `needs_dfg`: `false`
- `missing_capability`: `MUST_CHECK API 语义（zmalloc 族不失败）`
- `confidence`: `0.85`
- `reason`: `` src/sparsearray.c:432 `arSlice *d = arAllocAndTrack(…)` 后立即 `d->encoding`——arAllocAndTrack 走 zmalloc_usable（src/zmalloc.c:298-301，OOM 时 zmalloc_oom_handler 直接 panic），不会返回 NULL。已读源码核对；与 case 21 同族（redis 分配惯例）。 ``
- `annotator`: `AI 提议 + 人工确认（Claude Code 提议 / Kyber5323 逐条确认，2026-09-24）`
- `notes`: `源码核对：sparsearray.c:50-55 arAllocAndTrack → zmalloc_usable；zmalloc.c:301 if (!ptr) zmalloc_oom_handler(size)。`

### DFV1-llama.cpp-69-72c8281b11e5633b

- type: `4.6` / `resource_lifetime`
- status: `ambiguous` (confidence 0.4)
- subject: `subctx` in `ggml_vk_compute_forward`
- candidate: `E-DEFECT-d152fb8cd2b7ea37a6e5cc69`
- anchor: `event:d0574d42a2aeca9b8151e76607d23a958db62b8e519dfbce0f1e2408eb8bee75`
- source: `ggml/src/ggml-vulkan/ggml-vulkan.cpp:12344-12344`, `ggml/src/ggml-vulkan/ggml-vulkan.cpp:12347-12347`, `ggml/src/ggml-vulkan/ggml-vulkan.cpp:12377-12377`, `ggml/src/ggml-vulkan/ggml-vulkan.cpp:12379-12379`, `ggml/src/ggml-vulkan/ggml-vulkan.cpp:12382-12382`, `ggml/src/ggml-vulkan/ggml-vulkan.cpp:12383-12383`, `ggml/src/ggml-vulkan/ggml-vulkan.cpp:12384-12384`, `ggml/src/ggml-vulkan/ggml-vulkan.cpp:12349-12349`, `ggml/src/ggml-vulkan/ggml-vulkan.cpp:12353-12353`, `ggml/src/ggml-vulkan/ggml-vulkan.cpp:12357-12357`, `ggml/src/ggml-vulkan/ggml-vulkan.cpp:12361-12361`, `ggml/src/ggml-vulkan/ggml-vulkan.cpp:12362-12362`, `ggml/src/ggml-vulkan/ggml-vulkan.cpp:12369-12369`, `ggml/src/ggml-vulkan/ggml-vulkan.cpp:12372-12372`, `ggml/src/ggml-vulkan/ggml-vulkan.cpp:12373-12373`, `ggml/src/ggml-vulkan/ggml-vulkan.cpp:12363-12363`, `ggml/src/ggml-vulkan/ggml-vulkan.cpp:12367-12367`, `ggml/src/ggml-vulkan/ggml-vulkan.cpp:12364-12364`, `ggml/src/ggml-vulkan/ggml-vulkan.cpp:12365-12365`

Facts:

- `event`='LOCK', `matched_name`='lock', `method`='ggml_vk_compute_forward', `point`='event:d0574d42a2aeca9b8151e76607d23a958db62b8e519dfbce0f1e2408eb8bee75', `source`='ggml/src/ggml-vulkan/ggml-vulkan.cpp:12344-12344', `subject`='subctx', `subject_from`='assignment'
- `event`='DEREFERENCE', `matched_name`='subctx->seqs', `method`='ggml_vk_compute_forward', `point`='event:5a8fb5194f8b2ce52f8f100cb04a2a768f17d97faadf676d3d574a96d6678668', `source`='ggml/src/ggml-vulkan/ggml-vulkan.cpp:12347-12347', `subject`='subctx', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='ggml_vk_compute_forward', `point`='event:2456a53e08f32496c8a8dfdc01a16c78faed7d349d0627b7ebd62d58e6c2b14e', `source`='ggml/src/ggml-vulkan/ggml-vulkan.cpp:12347-12347', `subject`='subctx', `subject_from`='receiver'
- `event`='CHECK', `matched_name`='!subctx->seqs.empty()', `method`='ggml_vk_compute_forward', `point`='event:ad762aec552d95463830d747bb2773ec35869b8230442e932d268fd1a6e0f626', `source`='ggml/src/ggml-vulkan/ggml-vulkan.cpp:12347-12347', `subject`='', `subject_from`=''
- `event`='DEREFERENCE', `matched_name`='subctx->exit_tensor_idx', `method`='ggml_vk_compute_forward', `point`='event:f9b736b55b5a3fdebb64d126dc606f0474aa8851e1bae4936f6540268a7bcc92', `source`='ggml/src/ggml-vulkan/ggml-vulkan.cpp:12377-12377', `subject`='subctx', `subject_from`=''
- `event`='BRANCH', `matched_name`='tensor_idx==subctx->exit_tensor_idx', `method`='ggml_vk_compute_forward', `point`='event:fc224c2a30a6a9608c1e7583ea245d9136e4ba82ab0800bb4053c3ed72fe7a25', `source`='ggml/src/ggml-vulkan/ggml-vulkan.cpp:12377-12377', `subject`='', `subject_from`=''
- `event`='DEREFERENCE', `matched_name`='subctx->out_memcpys', `method`='ggml_vk_compute_forward', `point`='event:386835685ba05c43878f4ea4837aaed29b9efbd0a106a32e6cf01d1abd8e1b36', `source`='ggml/src/ggml-vulkan/ggml-vulkan.cpp:12379-12379', `subject`='subctx', `subject_from`=''
- `event`='BRANCH', `matched_name`='subctx->out_memcpys', `method`='ggml_vk_compute_forward', `point`='event:fb107124749a625828a3568b480ecc355f02fb1c90a8fbdd677f29a64a114041', `source`='ggml/src/ggml-vulkan/ggml-vulkan.cpp:12379-12379', `subject`='', `subject_from`=''
- `event`='DEREFERENCE', `matched_name`='subctx->in_memcpys', `method`='ggml_vk_compute_forward', `point`='event:2bfc322e065ba10c41e12cb9a5c8ba35cfd7be2011d530985b9151025fa81364', `source`='ggml/src/ggml-vulkan/ggml-vulkan.cpp:12382-12382', `subject`='subctx', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='ggml_vk_compute_forward', `point`='event:86f62c9af6401f93bb6c229c717e87db12a3b181db779e69e7af2d5a01f6ce99', `source`='ggml/src/ggml-vulkan/ggml-vulkan.cpp:12382-12382', `subject`='subctx', `subject_from`='receiver'
- `event`='DEREFERENCE', `matched_name`='subctx->out_memcpys', `method`='ggml_vk_compute_forward', `point`='event:8c2fbfdbf968ed887cf2d31ac0915132ca6f00af03af913b3d5aae61b0ac09a6', `source`='ggml/src/ggml-vulkan/ggml-vulkan.cpp:12383-12383', `subject`='subctx', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='ggml_vk_compute_forward', `point`='event:5532feaf9c7c659a9857dd6fcf11ca647bc551e8af379dd25cd55d41f029c073', `source`='ggml/src/ggml-vulkan/ggml-vulkan.cpp:12383-12383', `subject`='subctx', `subject_from`='receiver'
- `event`='DEREFERENCE', `matched_name`='subctx->memsets', `method`='ggml_vk_compute_forward', `point`='event:cd0aa8a0eb9a34847a9365d9cf10770596886c2808eacbe358960c965b1d1124', `source`='ggml/src/ggml-vulkan/ggml-vulkan.cpp:12384-12384', `subject`='subctx', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='ggml_vk_compute_forward', `point`='event:5b74c63b883bfe20abd2afc334e499c380f6ef36b895f33d4d136b9f6356c61d', `source`='ggml/src/ggml-vulkan/ggml-vulkan.cpp:12384-12384', `subject`='subctx', `subject_from`='receiver'
- `event`='CALL', `matched_name`='call_expression', `method`='ggml_vk_compute_forward', `point`='event:7d35e73743346fee177889fc0db0dda2af35d296202ff045e55127a7f3829e1c', `source`='ggml/src/ggml-vulkan/ggml-vulkan.cpp:12349-12349', `subject`='ctx', `subject_from`='argument'
- `event`='DEREFERENCE', `matched_name`='subctx->in_memcpys', `method`='ggml_vk_compute_forward', `point`='event:dd155f5ac222602415426ee8722fc07d685bdcea3ccc8212f44b42f574f35a06', `source`='ggml/src/ggml-vulkan/ggml-vulkan.cpp:12353-12353', `subject`='subctx', `subject_from`=''
- `event`='BRANCH', `matched_name`='subctx->in_memcpys', `method`='ggml_vk_compute_forward', `point`='event:1ff8d1461dd81cee640b96fd14749bab089f1aa62366faa323f24e28058fe654', `source`='ggml/src/ggml-vulkan/ggml-vulkan.cpp:12353-12353', `subject`='', `subject_from`=''
- `event`='DEREFERENCE', `matched_name`='subctx->memsets', `method`='ggml_vk_compute_forward', `point`='event:9ae0a6a286e763d2cac69558437bd4429311f118580e0c731bf62ff3213dd9a5', `source`='ggml/src/ggml-vulkan/ggml-vulkan.cpp:12357-12357', `subject`='subctx', `subject_from`=''
- `event`='BRANCH', `matched_name`='subctx->memsets', `method`='ggml_vk_compute_forward', `point`='event:627926dd214cbaf9b131048da123927ba49b013f4939dbe5b9d4e4d52464dac5', `source`='ggml/src/ggml-vulkan/ggml-vulkan.cpp:12357-12357', `subject`='', `subject_from`=''
- `event`='DEREFERENCE', `matched_name`='ctx->device', `method`='ggml_vk_compute_forward', `point`='event:10b2490a5f4d9bd2d7bfc36cf9dbfc1c640572a7fbca10eb98129f891461b156', `source`='ggml/src/ggml-vulkan/ggml-vulkan.cpp:12361-12361', `subject`='ctx', `subject_from`=''
- `event`='DEREFERENCE', `matched_name`='ctx->device->serialize_submissions', `method`='ggml_vk_compute_forward', `point`='event:281b0a4bb468c5f4d2c2586d4b8679e282ff33e4c0111279f904251bcfbb59bb', `source`='ggml/src/ggml-vulkan/ggml-vulkan.cpp:12361-12361', `subject`='ctx', `subject_from`=''
- `event`='BRANCH', `matched_name`='ctx->device->serialize_submissions', `method`='ggml_vk_compute_forward', `point`='event:64b8b7694cdda080bdce6e168a97aaa79228d5010fdbee3ace925e439ab1b627', `source`='ggml/src/ggml-vulkan/ggml-vulkan.cpp:12361-12361', `subject`='', `subject_from`=''
- `event`='DEREFERENCE', `matched_name`='ctx->fence', `method`='ggml_vk_compute_forward', `point`='event:bbc9b4b2fd951097b33b1ceab10c21c07ffb3030b11c3260d08da792d01d61a3', `source`='ggml/src/ggml-vulkan/ggml-vulkan.cpp:12362-12362', `subject`='ctx', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='ggml_vk_compute_forward', `point`='event:16e085eb96e45404798c6948441e33f8f9e1f5ddfe4e739558f8249410d5fddd', `source`='ggml/src/ggml-vulkan/ggml-vulkan.cpp:12362-12362', `subject`='subctx', `subject_from`='argument'
- `event`='DEREFERENCE', `matched_name`='ctx->submit_pending', `method`='ggml_vk_compute_forward', `point`='event:9c8ccfba1ee620dd80998975b1c6e0f9c84ef768b824fcd865d75cd87374713a', `source`='ggml/src/ggml-vulkan/ggml-vulkan.cpp:12369-12369', `subject`='ctx', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='ggml_vk_compute_forward', `point`='event:3d2e50894fa2edf50326ac5a8ac8f5a21d40a33fb083bdf4ead8daeb22cba144', `source`='ggml/src/ggml-vulkan/ggml-vulkan.cpp:12372-12372', `subject`='ctx', `subject_from`='argument'
- `event`='CALL', `matched_name`='call_expression', `method`='ggml_vk_compute_forward', `point`='event:601863be4f77be4212d047bacdeb701d0a0c157c97a03926ee81e8865ee458fb', `source`='ggml/src/ggml-vulkan/ggml-vulkan.cpp:12373-12373', `subject`='ctx', `subject_from`='argument'
- `event`='DEREFERENCE', `matched_name`='ctx->almost_ready_fence_pending', `method`='ggml_vk_compute_forward', `point`='event:9bf78e02290ef93b1d5327ec352029e5a26cde8bd1a1f95449890ce4d5463f3e', `source`='ggml/src/ggml-vulkan/ggml-vulkan.cpp:12363-12363', `subject`='ctx', `subject_from`=''
- `event`='BRANCH', `matched_name`='almost_ready&&!ctx->almost_ready_fence_pending', `method`='ggml_vk_compute_forward', `point`='event:360ff3dba633505a81653e0cbc8f34ea5b46cd83930f34a7a4842336374fa364', `source`='ggml/src/ggml-vulkan/ggml-vulkan.cpp:12363-12363', `subject`='', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='ggml_vk_compute_forward', `point`='event:b7b24455cf916d7b0649329b10fe2c1f509898ea287c4401924e5834ce909e77', `source`='ggml/src/ggml-vulkan/ggml-vulkan.cpp:12367-12367', `subject`='subctx', `subject_from`='argument'
- `event`='DEREFERENCE', `matched_name`='ctx->almost_ready_fence', `method`='ggml_vk_compute_forward', `point`='event:87d0ac25548553e5c0013c64d95ef92b0f83503419dbe9ca7c3f50a5073fbe1e', `source`='ggml/src/ggml-vulkan/ggml-vulkan.cpp:12364-12364', `subject`='ctx', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='ggml_vk_compute_forward', `point`='event:c160f927453cb5b1e1732af6da19451fa69129f8d0497af5377a2d3b372c4bbb', `source`='ggml/src/ggml-vulkan/ggml-vulkan.cpp:12364-12364', `subject`='subctx', `subject_from`='argument'
- `event`='DEREFERENCE', `matched_name`='ctx->almost_ready_fence_pending', `method`='ggml_vk_compute_forward', `point`='event:fae5e623ff7bda390509fd6c01d8b364be5d4c57dcd2a667da6f0e2cceb448d1', `source`='ggml/src/ggml-vulkan/ggml-vulkan.cpp:12365-12365', `subject`='ctx', `subject_from`=''

Uncertain facts:

- `decisive`=True, `detail`='the lock was recovered from a receiver or a declaration and its declared type is not a known mutex, so the name identifies an expression rather than the object it names', `fact`='LOCK_IDENTITY', `point`='event:d0574d42a2aeca9b8151e76607d23a958db62b8e519dfbce0f1e2408eb8bee75', `source`='ggml/src/ggml-vulkan/ggml-vulkan.cpp:12344-12344', `status`='ambiguous'

Missing evidence:

- the ownership contract of a callee: whether a function the resource is passed to takes ownership, borrows it, or frees it
- RAII and smart pointers: `unique_ptr<T> p(new T)` is an ALLOC with no RELEASE in the graph, so every owning smart pointer reads as a leak
- aliasing and reassignment: `q = p; free(q)`, `free(p); p = NULL` and `p = realloc(p, n)` are all invisible without a data-flow graph
- a release performed inside a callee: the graph only sees RELEASE points in the method it walked
- whether the error path is reachable in practice: the graph reports the path exists, not that the input reaches it
- whether a destructor or another path releases the lock first
- the guard-to-mutex binding: `std::unique_lock<T> g(m)` releases through `g`, and `g.unlock()` is an operation on the guard, not on `m`, so a walk that joins on the mutex's identity cannot see the release

```cpp
    VK_LOG_DEBUG("ggml_vk_compute_forward(" << tensor << ", name=" << tensor->name << ", op=" << ggml_op_name(tensor->op) << ", type=" << tensor->type << ", ne0=" << tensor->ne[0] << ", ne1=" << tensor->ne[1] << ", ne2=" << tensor->ne[2] << ", ne3=" << tensor->ne[3] << ", nb0=" << tensor->nb[0] << ", nb1=" << tensor->nb[1] << ", nb2=" << tensor->nb[2] << ", nb3=" << tensor->nb[3] << ", view_src=" << tensor->view_src << ", view_offs=" << tensor->view_offs << ")");

    vk_context subctx = ctx->tensor_ctxs[tensor_idx].lock();

    // Only run if ctx hasn't been submitted yet
```

```cpp

    // Only run if ctx hasn't been submitted yet
    if (!subctx->seqs.empty()) {
#ifdef GGML_VULKAN_CHECK_RESULTS
        ggml_vk_check_results_0(ctx, cgraph, tensor_idx);
```

```cpp
    }

    if (tensor_idx == subctx->exit_tensor_idx) {
        // Do staging buffer copies
        for (auto& cpy : subctx->out_memcpys) {
```

```cpp
    if (tensor_idx == subctx->exit_tensor_idx) {
        // Do staging buffer copies
        for (auto& cpy : subctx->out_memcpys) {
            memcpy(cpy.dst, cpy.src, cpy.n);
        }
```

```cpp
            memcpy(cpy.dst, cpy.src, cpy.n);
        }
        subctx->in_memcpys.clear();
        subctx->out_memcpys.clear();
        subctx->memsets.clear();
```

```cpp
        }
        subctx->in_memcpys.clear();
        subctx->out_memcpys.clear();
        subctx->memsets.clear();
    }
```

```cpp
        subctx->in_memcpys.clear();
        subctx->out_memcpys.clear();
        subctx->memsets.clear();
    }
}
```

```cpp
    if (!subctx->seqs.empty()) {
#ifdef GGML_VULKAN_CHECK_RESULTS
        ggml_vk_check_results_0(ctx, cgraph, tensor_idx);
#endif

```

```cpp

        // Do staging buffer copies
        for (auto& cpy : subctx->in_memcpys) {
            memcpy(cpy.dst, cpy.src, cpy.n);
        }
```

```cpp
        }

        for (auto& mset : subctx->memsets) {
            memset(mset.dst, mset.val, mset.n);
        }
```

```cpp
        }

        if (ctx->device->serialize_submissions) {
            ggml_vk_submit(subctx, ctx->fence);
        } else if (almost_ready && !ctx->almost_ready_fence_pending) {
```

```cpp

        if (ctx->device->serialize_submissions) {
            ggml_vk_submit(subctx, ctx->fence);
        } else if (almost_ready && !ctx->almost_ready_fence_pending) {
            ggml_vk_submit(subctx, ctx->almost_ready_fence);
```

```cpp
            ggml_vk_submit(subctx, {});
        }
        ctx->submit_pending = true;

#ifdef GGML_VULKAN_CHECK_RESULTS
```

```cpp

#ifdef GGML_VULKAN_CHECK_RESULTS
        ggml_vk_synchronize(ctx);
        ggml_vk_check_results_1(ctx, cgraph, tensor_idx);
#endif
```

```cpp
#ifdef GGML_VULKAN_CHECK_RESULTS
        ggml_vk_synchronize(ctx);
        ggml_vk_check_results_1(ctx, cgraph, tensor_idx);
#endif
    }
```

```cpp
        if (ctx->device->serialize_submissions) {
            ggml_vk_submit(subctx, ctx->fence);
        } else if (almost_ready && !ctx->almost_ready_fence_pending) {
            ggml_vk_submit(subctx, ctx->almost_ready_fence);
            ctx->almost_ready_fence_pending = true;
```

```cpp
            ctx->almost_ready_fence_pending = true;
        } else {
            ggml_vk_submit(subctx, {});
        }
        ctx->submit_pending = true;
```

```cpp
            ggml_vk_submit(subctx, ctx->fence);
        } else if (almost_ready && !ctx->almost_ready_fence_pending) {
            ggml_vk_submit(subctx, ctx->almost_ready_fence);
            ctx->almost_ready_fence_pending = true;
        } else {
```

```cpp
        } else if (almost_ready && !ctx->almost_ready_fence_pending) {
            ggml_vk_submit(subctx, ctx->almost_ready_fence);
            ctx->almost_ready_fence_pending = true;
        } else {
            ggml_vk_submit(subctx, {});
```

Verdict (recorded in `defect_review.json`):

- `verdict`: `REVIEWED`
- `is_true_positive`: `false` — **false positive**
- `failure_reason`: `extractor`
- `needs_dfg`: `false`
- `missing_capability`: `.lock() 消歧（weak_ptr::lock vs mutex）`
- `confidence`: `0.85`
- `reason`: `` ggml/src/ggml-vulkan/ggml-vulkan.cpp:12344 `vk_context subctx = ctx->tensor_ctxs[tensor_idx].lock()`——这是共享槽位的 `.lock()`（取共享所有权，弱引用升格），不是互斥量加锁；LOCK 事件按成员名匹配把 `.lock()` 误判为 mutex 获取，随后的「未释放」自然不成立（单子的 MUTEX_RECURSIVE 类不确定项也已自述 type not a known mutex）。 ``
- `annotator`: `AI 提议 + 人工确认（Claude Code 提议 / Kyber5323 逐条确认，2026-09-24）`
- `notes`: `null`

### DFV1-llama.cpp-69-0416083f00f39aae

- type: `4.6` / `resource_lifetime`
- status: `resolved` (confidence 0.6)
- subject: `ggml_critical_section_mutex` in `ggml_critical_section_start`
- candidate: `E-DEFECT-191b374c0040e41e4ea8b556`
- anchor: `event:2d2d0ce6cfba9bc6c82bc641caae21270a9bb90cd38537fb5f172b3a8e914ed9`
- source: `ggml/src/ggml-threading.cpp:7-7`

Facts:

- `event`='LOCK', `matched_name`='lock', `method`='ggml_critical_section_start', `point`='event:2d2d0ce6cfba9bc6c82bc641caae21270a9bb90cd38537fb5f172b3a8e914ed9', `source`='ggml/src/ggml-threading.cpp:7-7', `subject`='ggml_critical_section_mutex', `subject_from`='receiver'

Uncertain facts:


Missing evidence:

- the ownership contract of a callee: whether a function the resource is passed to takes ownership, borrows it, or frees it
- RAII and smart pointers: `unique_ptr<T> p(new T)` is an ALLOC with no RELEASE in the graph, so every owning smart pointer reads as a leak
- aliasing and reassignment: `q = p; free(q)`, `free(p); p = NULL` and `p = realloc(p, n)` are all invisible without a data-flow graph
- a release performed inside a callee: the graph only sees RELEASE points in the method it walked
- whether the error path is reachable in practice: the graph reports the path exists, not that the input reaches it
- whether a destructor or another path releases the lock first
- the guard-to-mutex binding: `std::unique_lock<T> g(m)` releases through `g`, and `g.unlock()` is an operation on the guard, not on `m`, so a walk that joins on the mutex's identity cannot see the release

```cpp

void ggml_critical_section_start() {
    ggml_critical_section_mutex.lock();
}

```

Verdict (recorded in `defect_review.json`):

- `verdict`: `REVIEWED`
- `is_true_positive`: `false` — **false positive**
- `failure_reason`: `ownership`
- `needs_dfg`: `false`
- `missing_capability`: `跨帧配对释放（paired unlock in callee）`
- `confidence`: `0.9`
- `reason`: `ggml/src/ggml-threading.cpp:7 该函数的职责就是加锁并持锁返回，释放由配对的 ggml_critical_section_end（另一帧）承担——函数对式协议，「另一帧的释放」。`
- `annotator`: `AI 提议 + 人工确认（Claude Code 提议 / Kyber5323 逐条确认，2026-09-24）`
- `notes`: `null`

### DFV1-llama.cpp-69-78e006bad06618d6

- type: `9.1` / `error_handling`
- status: `ambiguous` (confidence 0.2)
- subject: `layer` in `llama_model_qwen3vlmoe.load_arch_tensors`
- candidate: `E-DEFECT-2dbc906ed5f6d20f1f094e6c`
- anchor: `event:5bc9071d4dcedfadf4a9b17c422dbd7bf9aa0f809c125b6f4952da5d72b95662`
- source: `src/models/qwen3vlmoe.cpp:56-56`

Facts:

- `event`='CALL', `matched_name`='call_expression', `method`='llama_model_qwen3vlmoe.load_arch_tensors', `point`='event:5bc9071d4dcedfadf4a9b17c422dbd7bf9aa0f809c125b6f4952da5d72b95662', `source`='src/models/qwen3vlmoe.cpp:56-56', `subject`='layer', `subject_from`='assignment'

Uncertain facts:

- `decisive`=True, `detail`='the name is the root of a longer expression (`s` in `s->buf`), so the same name elsewhere may be a different object', `fact`='SUBJECT_PATH', `point`='event:5bc9071d4dcedfadf4a9b17c422dbd7bf9aa0f809c125b6f4952da5d72b95662', `source`='src/models/qwen3vlmoe.cpp:56-56', `status`='ambiguous'
- `count`=1, `decisive`=False, `detail`="the walk passed branch points that sit before the call's own line; a check on a value cannot precede the value, so they were not read as examinations of it", `fact`='BACKWARD_BRANCH_SKIPPED', `status`='ambiguous'

Missing evidence:

- which APIs must have their result checked: the graph sees a call whose result was stored and not examined, and whether this call has a failure mode worth checking is API semantics the AI side holds
- whether a result was discarded outright: a CALL point records where a result went, never that it went nowhere, so `f();` as a bare statement is outside this query entirely -- and so is a result passed straight on as an argument, which no branch examines and no variable holds

```cpp
        layer.ffn_gate_exps = create_tensor(tn(LLM_TENSOR_FFN_GATE_EXPS, "weight", i), {  n_embd, n_ff_exp, n_expert}, 0);
        layer.ffn_down_exps = create_tensor(tn(LLM_TENSOR_FFN_DOWN_EXPS, "weight", i), {n_ff_exp,   n_embd, n_expert}, 0);
        layer.ffn_up_exps   = create_tensor(tn(LLM_TENSOR_FFN_UP_EXPS,   "weight", i), {  n_embd, n_ff_exp, n_expert}, 0);
    }
}
```

Verdict (recorded in `defect_review.json`):

- `verdict`: `REVIEWED`
- `is_true_positive`: `false` — **false positive**
- `failure_reason`: `contract`
- `needs_dfg`: `false`
- `missing_capability`: `MUST_CHECK API 语义（create_tensor 必需位不失败）`
- `confidence`: `0.75`
- `reason`: `` src/models/qwen3vlmoe.cpp:56 `layer.ffn_up_exps = create_tensor(tn(…), {…}, 0);`——flags=0 的必需 tensor，create_tensor 找不到时内部 abort（llama.cpp 建模惯例），不会静默返回 NULL 再被存进 layer。与 case 21/23 同族。 ``
- `annotator`: `AI 提议 + 人工确认（Claude Code 提议 / Kyber5323 逐条确认，2026-09-24）`
- `notes`: `null`

### DFV1-llama.cpp-69-2f7db7656b4287cb

- type: `9.1` / `error_handling`
- status: `resolved` (confidence 0.4)
- subject: `iacc_1` in `ggml_gemv_q4_K_8x8_q8_K`
- candidate: `E-DEFECT-ff8bf3caa491640d9c40aa3c`
- anchor: `event:64b65f4751a93979315fb7bcee82017ac1c1ea34ff4dc1a704f76071ebaf80ff`
- source: `ggml/src/ggml-cpu/arch/x86/repack.cpp:1646-1646`

Facts:

- `event`='CALL', `matched_name`='call_expression', `method`='ggml_gemv_q4_K_8x8_q8_K', `point`='event:64b65f4751a93979315fb7bcee82017ac1c1ea34ff4dc1a704f76071ebaf80ff', `source`='ggml/src/ggml-cpu/arch/x86/repack.cpp:1646-1646', `subject`='iacc_1', `subject_from`='assignment'

Uncertain facts:

- `count`=4, `decisive`=False, `detail`="the walk passed branch points that sit before the call's own line; a check on a value cannot precede the value, so they were not read as examinations of it", `fact`='BACKWARD_BRANCH_SKIPPED', `status`='ambiguous'

Missing evidence:

- which APIs must have their result checked: the graph sees a call whose result was stored and not examined, and whether this call has a failure mode worth checking is API semantics the AI side holds
- whether a result was discarded outright: a CALL point records where a result went, never that it went nowhere, so `f();` as a bare statement is outside this query entirely -- and so is a result passed straight on as an argument, which no branch examines and no variable holds

```cpp

                    iacc_1 = _mm256_add_epi16(iacc_1, _mm256_maddubs_epi16(_mm256_blend_epi32(rhs_vec_0123_12 ,_mm256_shuffle_epi32(rhs_vec_4567_12, 177), 170), _mm256_shuffle_epi32(lhs_vec_11, 0)));
                    iacc_1 = _mm256_add_epi16(iacc_1, _mm256_maddubs_epi16(_mm256_blend_epi32(_mm256_shuffle_epi32(rhs_vec_0123_12, 177) ,rhs_vec_4567_12, 170), _mm256_shuffle_epi32(lhs_vec_11, 85)));

                    iacc_1 = _mm256_add_epi16(iacc_1, _mm256_maddubs_epi16(_mm256_blend_epi32(rhs_vec_0123_13 ,_mm256_shuffle_epi32(rhs_vec_4567_13, 177), 170), _mm256_shuffle_epi32(lhs_vec_11, 170)));
```

Verdict (recorded in `defect_review.json`):

- `verdict`: `REVIEWED`
- `is_true_positive`: `false` — **false positive**
- `failure_reason`: `contract`
- `needs_dfg`: `false`
- `missing_capability`: `MUST_CHECK 词表排除纯计算调用（intrinsics）`
- `confidence`: `0.9`
- `reason`: `` ggml/src/ggml-cpu/arch/x86/repack.cpp:1646 `iacc_1 = _mm256_add_epi16(iacc_1, _mm256_maddubs_epi16(…))`——SIMD 内建函数的返回值是算术结果，没有失败语义，「未检查返回值」对内建函数不成立；9.1 候选规则需在词表排除内建函数/纯计算调用。 ``
- `annotator`: `AI 提议 + 人工确认（Claude Code 提议 / Kyber5323 逐条确认，2026-09-24）`
- `notes`: `null`

### DFV1-llama.cpp-69-d2f307ac373ad159

- type: `1.1` / `race_condition`
- status: `ambiguous` (confidence 0.5)
- subject: `res` in `llama_model_gemma4.graph.graph`
- candidate: `E-DEFECT-9da6683db58ebb5a6eabc494`
- anchor: `event:b5edb62d3fd58ec7af83ea198a904303d75158db076bd2d72b8e0fbd2fdb40f0`
- source: `src/models/gemma4.cpp:433-433`, `src/models/starcoder2.cpp:155-155`

Facts:

- `event`='WRITE', `matched_name`='res->t_logits', `method`='llama_model_gemma4.graph.graph', `point`='event:b5edb62d3fd58ec7af83ea198a904303d75158db076bd2d72b8e0fbd2fdb40f0', `source`='src/models/gemma4.cpp:433-433', `subject`='res', `subject_from`=''
- `event`='WRITE', `matched_name`='res->t_logits', `method`='llama_model_starcoder2.graph.graph', `point`='event:d340e6ae4e5ea7bc105e36a2dac2df6a7ae2cb7627a928a525c40e5ff8acd8d1', `source`='src/models/starcoder2.cpp:155-155', `subject`='res', `subject_from`=''

Uncertain facts:

- `decisive`=True, `detail`='the repository starts threads somewhere, so neither method is ordered', `fact`='MAY_PARALLEL', `status`='ambiguous'
- `decisive`=True, `detail`='no lock in this method', `fact`='PROTECTED_BY', `lock`='', `method`='llama_model_starcoder2.graph.graph', `status`='ambiguous'
- `decisive`=True, `detail`='no lock in this method', `fact`='PROTECTED_BY', `lock`='', `method`='llama_model_gemma4.graph.graph', `status`='ambiguous'
- `decisive`=True, `detail`='access kind from READ/WRITE events: write vs write', `fact`='ACCESS_KIND', `status`='ambiguous'

Missing evidence:

- whether the two contexts really run concurrently: MAY_PARALLEL is undecidable from spawn points alone
- whether the lock actually guards this variable: holding a lock near an access is not the same as the lock protecting the data
- whether the variable tolerates weak consistency (a statistics counter may be allowed to race by design)

```cpp

    cb(cur, "result_output", -1);
    res->t_logits = cur;

    ggml_build_forward_expand(gf, cur);
```

```cpp

    cb(cur, "result_output", -1);
    res->t_logits = cur;

    ggml_build_forward_expand(gf, cur);
```

Verdict (recorded in `defect_review.json`):

- `verdict`: `REVIEWED`
- `is_true_positive`: `false` — **false positive**
- `failure_reason`: `identity`
- `needs_dfg`: `false`
- `missing_capability`: `声明身份与作用域（member-keeping identity 需类型限定）`
- `confidence`: `0.85`
- `reason`: `` src/models/gemma4.cpp:433 的 `res->t_logits` 写点与 src/models/starcoder2.cpp:155 的写点是两个不同模型类 graph_build 里各自的 res 局部对象——共享身份 name:res#t_logits 仅按拼写+成员连接；两个方法分属不同模型的建图路径，对象从不共享。 ``
- `annotator`: `AI 提议 + 人工确认（Claude Code 提议 / Kyber5323 逐条确认，2026-09-24）`
- `notes`: `null`

### DFV1-redis-50-6dd6515b7ed62c28

- type: `1.1` / `race_condition`
- status: `ambiguous` (confidence 0.5)
- subject: `s` in `streamCreateConsumer`
- candidate: `E-DEFECT-63b32fcbb5d930dbdbd110c8`
- anchor: `event:0e3369566c28e23ca3721479993df4073806ff30b2ecdc9a700bd7863ae7417b`
- source: `src/t_stream.c:3508-3508`, `src/t_stream.c:3506-3506`, `src/t_stream.c:3365-3365`

Facts:

- `event`='WRITE', `matched_name`='s->alloc_size', `method`='streamCreateConsumer', `point`='event:0e3369566c28e23ca3721479993df4073806ff30b2ecdc9a700bd7863ae7417b', `source`='src/t_stream.c:3508-3508', `subject`='s', `subject_from`=''
- `event`='WRITE', `matched_name`='s->alloc_size', `method`='streamCreateConsumer', `point`='event:4f0de55084c62a9c48dfbff87fb03adb11aa385d8b9e84b10009e88927768d58', `source`='src/t_stream.c:3506-3506', `subject`='s', `subject_from`=''
- `event`='WRITE', `matched_name`='s->alloc_size', `method`='streamFreeNACK', `point`='event:77a30afddce7771737125b116cd90461aa2308d498623ab0452f0f6fe68c3686', `source`='src/t_stream.c:3365-3365', `subject`='s', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='streamCreateConsumer', `point`='event:a962af8f858b66f084a21ddfede576c2d310cb6b55219aadbef0fad2da7436ab', `source`='src/t_stream.c:3508-3508', `subject`='s', `subject_from`='assignment'

Uncertain facts:

- `decisive`=True, `detail`='the repository starts threads somewhere, so neither method is ordered', `fact`='MAY_PARALLEL', `status`='ambiguous'
- `decisive`=True, `detail`='no lock in this method', `fact`='PROTECTED_BY', `lock`='', `method`='streamFreeNACK', `status`='ambiguous'
- `decisive`=True, `detail`='no lock in this method', `fact`='PROTECTED_BY', `lock`='', `method`='streamCreateConsumer', `status`='ambiguous'
- `decisive`=True, `detail`='access kind from READ/WRITE events: write vs write', `fact`='ACCESS_KIND', `status`='ambiguous'

Missing evidence:

- whether the two contexts really run concurrently: MAY_PARALLEL is undecidable from spawn points alone
- whether the lock actually guards this variable: holding a lock near an access is not the same as the lock protecting the data
- whether the variable tolerates weak consistency (a statistics counter may be allowed to race by design)

```c
    s->alloc_size += usable;
    consumer->name = sdsdup(name);
    s->alloc_size += sdsAllocSize(consumer->name);
    consumer->pel = raxNewEx(0, &s->alloc_size, sizeof(streamID));
    consumer->active_time = -1;
```

```c
        return NULL;
    }
    s->alloc_size += usable;
    consumer->name = sdsdup(name);
    s->alloc_size += sdsAllocSize(consumer->name);
```

```c
    size_t usable;
    zfree_usable(na, &usable);
    s->alloc_size -= usable;
}

```

Verdict (recorded in `defect_review.json`):

- `verdict`: `REVIEWED`
- `is_true_positive`: `false` — **false positive**
- `failure_reason`: `other`
- `needs_dfg`: `false`
- `missing_capability`: `执行模型事实（single-thread / GIL）`
- `confidence`: `0.7`
- `reason`: `src/t_stream.c:3508/3506（streamCreateConsumer，+alloc_size）与 :3365（streamFreeNACK，−alloc_size）操作同一 stream 的计数器，但 redis 命令执行在主线程单线程进行（模块侧访问需持 GIL），两条路径不并发——图缺「单线程执行模型」事实，MAY_PARALLEL 歧义在图内无法排除。`
- `annotator`: `AI 提议 + 人工确认（Claude Code 提议 / Kyber5323 逐条确认，2026-09-24）`
- `notes`: `null`

### DFV1-llama.cpp-69-9211f82d2999ee21

- type: `1.4` / `lock_order`
- status: `ambiguous` (confidence 0.3)
- subject: `mutex` in `server_models.on_child_exit`
- candidate: `E-DEFECT-615c6f1bb065b328aa76d920`
- anchor: `event:12422b0b410856f30477be441d1a6e77cfedfa37385ab17bc53ceea2b8ecaa94`
- source: `tools/server/server-models.cpp:1223-1223`, `tools/server/server-models.cpp:1232-1232`

Facts:

- `holding_confirmed`=True, `lock`='mutex', `method`='server_models.on_child_exit', `method_symbol_id`='symbol:fdc1fd376630eaccc7031d357ac9a509522f0a7cc4f9426b0934113fe0103ebd', `point`='event:12422b0b410856f30477be441d1a6e77cfedfa37385ab17bc53ceea2b8ecaa94', `role`='first', `side`='single', `source`='tools/server/server-models.cpp:1223-1223'
- `holding_confirmed`=True, `lock`='mutex', `method`='server_models.on_child_exit', `method_symbol_id`='symbol:fdc1fd376630eaccc7031d357ac9a509522f0a7cc4f9426b0934113fe0103ebd', `point`='event:10c2a837d6a5b616f9a2de2319773068d5f186a56aadae64ae2b33e0c3cb3c13', `role`='second', `side`='single', `source`='tools/server/server-models.cpp:1232-1232'

Uncertain facts:

- `decisive`=False, `detail`='a recursive mutex (PTHREAD_MUTEX_RECURSIVE) makes a second acquisition by the same thread legal', `fact`='MUTEX_RECURSIVE', `status`='ambiguous'
- `decisive`=True, `detail`='the lock was recovered from a receiver or a declaration and its declared type is not a known mutex, so the name identifies an expression rather than the object it names', `fact`='LOCK_IDENTITY', `status`='ambiguous'
- `decisive`=True, `detail`='no declaration of this name in this file, so the identity falls back to the spelling -- a macro, an enum constant, or a global declared in a header the parser does not read', `fact`='IDENTITY_UNKNOWN', `point`='event:12422b0b410856f30477be441d1a6e77cfedfa37385ab17bc53ceea2b8ecaa94', `source`='tools/server/server-models.cpp:1223-1223', `status`='ambiguous'

Missing evidence:

- whether the mutex is recursive: under PTHREAD_MUTEX_RECURSIVE a second acquisition by the same thread is legal, and the graph does not hold the mutex's type
- the execution context: whether the two methods can run concurrently at all -- nothing in the graph orders them
- lock aliasing: two expressions naming the same mutex (`&s->m` and `s->m`) are two names here, and one name is one lock
- the failure path of a non-blocking acquisition: a try-lock that fails leaves the lock unheld and the graph cannot tell the two arms apart

```cpp
void server_models::on_child_exit(const std::string & name, const std::shared_ptr<server_subproc> & proc, server_child_mode mode, int exit_code) {
    {
        std::lock_guard<std::mutex> lk(mutex);
        stopping_models.erase(name);
        auto it = mapping.find(name);
```

```cpp
    if (mode == SERVER_CHILD_MODE_DOWNLOAD) {
        // instance will be cleaned up on next load_models() call
        std::lock_guard<std::mutex> lk(mutex);
        cv.notify_all();
    } else {
```

Verdict (recorded in `defect_review.json`):

- `verdict`: `REVIEWED`
- `is_true_positive`: `false` — **false positive**
- `failure_reason`: `ownership`
- `needs_dfg`: `false`
- `missing_capability`: `RAII 守卫的析构释放（destructor release）`
- `confidence`: `0.9`
- `reason`: `tools/server/server-models.cpp:1222-1229 第一个 lock_guard 在内层块内，块在 :1229 闭合即析构释放（其内还有 return 分支）；:1232 的第二个 lock_guard 是外层 if 内的独立作用域——顺序获取不嵌套。已读源码核对。析构释放无 UNLOCK 事件，holding_confirmed=true 是这一缺口的产物。`
- `annotator`: `AI 提议 + 人工确认（Claude Code 提议 / Kyber5323 逐条确认，2026-09-24）`
- `notes`: `源码核对：server-models.cpp:1229 内层块闭合在 :1232 的 if 之前。`

### DFV1-llama.cpp-69-67eb44dc900d1559

- type: `1.4` / `lock_order`
- status: `resolved` (confidence 0.6)
- subject: `mtx` in `console.spinner.start`
- candidate: `E-DEFECT-16954c434eb2550987fb81f4`
- anchor: `event:238243dbd28ebce2ab3cb7f3039416098b6194d10f33ef77884d4748f4a75702`
- source: `common/console.cpp:1109-1109`, `common/console.cpp:1119-1119`

Facts:

- `holding_confirmed`=True, `lock`='mtx', `method`='console.spinner.start', `method_symbol_id`='symbol:8019eeb9a9d0c12d3cb1e53b8f44010ff918954209780d6a4fa417f08bd8da3f', `point`='event:238243dbd28ebce2ab3cb7f3039416098b6194d10f33ef77884d4748f4a75702', `role`='first', `side`='single', `source`='common/console.cpp:1109-1109'
- `holding_confirmed`=True, `lock`='mtx', `method`='console.spinner.start', `method_symbol_id`='symbol:8019eeb9a9d0c12d3cb1e53b8f44010ff918954209780d6a4fa417f08bd8da3f', `point`='event:aa3035d5b7d874854b5976090b5e058fbcaea8860cc80e8d01b6351330da9582', `role`='second', `side`='single', `source`='common/console.cpp:1119-1119'

Uncertain facts:

- `decisive`=False, `detail`='a recursive mutex (PTHREAD_MUTEX_RECURSIVE) makes a second acquisition by the same thread legal', `fact`='MUTEX_RECURSIVE', `status`='ambiguous'

Missing evidence:

- whether the mutex is recursive: under PTHREAD_MUTEX_RECURSIVE a second acquisition by the same thread is legal, and the graph does not hold the mutex's type
- the execution context: whether the two methods can run concurrently at all -- nothing in the graph orders them
- lock aliasing: two expressions naming the same mutex (`&s->m` and `s->m`) are two names here, and one name is one lock
- the failure path of a non-blocking acquisition: a try-lock that fails leaves the lock unheld and the graph cannot tell the two arms apart

```cpp
        }
        void start() {
            std::unique_lock<std::mutex> lock(mtx);
            if (simple_io || running) {
                return;
```

```cpp
            running = true;
            th = std::thread([]() {
                std::unique_lock<std::mutex> lock(mtx);
                while (true) {
                    if (cv_stop.wait_for(lock, wait_time, []{ return !running; })) {
```

Verdict (recorded in `defect_review.json`):

- `verdict`: `REVIEWED`
- `is_true_positive`: `false` — **false positive**
- `failure_reason`: `other`
- `needs_dfg`: `false`
- `missing_capability`: `线程边界（lambda runs on new thread）`
- `confidence`: `0.9`
- `reason`: `` common/console.cpp:1109 start() 的 unique_lock 持至函数返回；:1119 的第二次获取在 `std::thread([](){ … })` 的 lambda 体内——在**新线程**上执行，新线程阻塞至 start() 返回、父线程放锁为止，是正常同步不是同线程自死锁。已读源码核对。图把 lambda 体当成同方法体，缺线程边界事实。 ``
- `annotator`: `AI 提议 + 人工确认（Claude Code 提议 / Kyber5323 逐条确认，2026-09-24）`
- `notes`: `源码核对：console.cpp:1118 th = std::thread([](){ … :1119 lock(mtx) … })。`
