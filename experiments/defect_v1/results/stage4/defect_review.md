# Defect candidate review — stage 4

- review id: `provenlattice-defect-v1-review`
- status: `DEFECT_REVIEW_READY_FOR_ANNOTATION` / claims `NO_PRECISION_VERDICT_YET`
- seed: `provenlattice-defect-v1`
- code: `0c27e011f91a324b327751fb5859193f64b4a719` (dirty working tree)
- sampling: repository x defect_key x resolution_status, round-robin, one sample across all repositories, target 20 (selected 20)

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
| llamacpp100 | `D:\代码理解\测试代码仓\llama.cpp-69\.provenlattice\codegraph.db` | 3 | `3cf03257f219` | 1000743 | lock_order=ok, race_condition=population, resource_lifetime=recall | yes (override) |
| redis50 | `D:\代码理解\测试代码仓\redis-50\.provenlattice\codegraph.db` | 3 | `5f08991bce47` | 557372 | lock_order=ok, race_condition=ok, resource_lifetime=recall | yes |

## Availability

| repository | key | status | population_N | sample_n | population capped | recall truncated |
|---|---|---|---|---|---|---|
| llamacpp100 | 1.1 | ambiguous | 1000000 | 2 | **yes** | yes |
| llamacpp100 | 4.1 | ambiguous | 221 | 2 | no | yes |
| llamacpp100 | 4.1 | resolved | 96 | 2 | no | yes |
| llamacpp100 | 4.1 | unresolved | 396 | 2 | no | yes |
| llamacpp100 | 4.6 | ambiguous | 20 | 1 | no | yes |
| llamacpp100 | 4.6 | resolved | 10 | 1 | no | yes |
| redis50 | 1.1 | ambiguous | 557315 | 2 | no | no |
| redis50 | 1.4 | ambiguous | 2 | 2 | no | no |
| redis50 | 4.1 | ambiguous | 23 | 2 | no | yes |
| redis50 | 4.1 | resolved | 12 | 2 | no | yes |
| redis50 | 4.6 | ambiguous | 16 | 1 | no | yes |
| redis50 | 4.6 | resolved | 4 | 1 | no | yes |

## Cases

### DFV1-llamacpp100-1998861e6bd08e1f

- type: `1.1` / `race_condition`
- status: `ambiguous` (confidence 0.5)
- subject: `n` in `make_test_cases_eval`
- candidate: `E-DEFECT-b2bedf74ec50da10e2a22a54`
- anchor: `event:072a949f247538926db727bede0d5fc438ac0d3564386120979dc0ca540f1eec`
- source: `tests/test-backend-ops.cpp:10036-10036`, `ggml/src/ggml-cpu/arch/s390/quants.c:567-567`

Facts:

- `event`='CALL', `matched_name`='call_expression', `method`='make_test_cases_eval', `point`='event:072a949f247538926db727bede0d5fc438ac0d3564386120979dc0ca540f1eec', `source`='tests/test-backend-ops.cpp:10036-10036', `subject`='n', `subject_from`='assignment'
- `event`='CALL', `matched_name`='call_expression', `method`='ggml_vec_dot_q5_0_q8_0', `point`='event:bda6bafa8188c31baf91dc37bc900cd55ce92868ec2261ea9ebd7aa75fbfa9ec', `source`='ggml/src/ggml-cpu/arch/s390/quants.c:567-567', `subject`='n', `subject_from`='argument'

Uncertain facts:

- `decisive`=True, `detail`='the repository starts threads somewhere, so neither method is ordered', `fact`='MAY_PARALLEL', `status`='ambiguous'
- `decisive`=True, `detail`='no lock in this method', `fact`='PROTECTED_BY', `lock`='', `method`='ggml_vec_dot_q5_0_q8_0', `status`='ambiguous'
- `decisive`=True, `detail`='no lock in this method', `fact`='PROTECTED_BY', `lock`='', `method`='make_test_cases_eval', `status`='ambiguous'
- `decisive`=True, `detail`='write/read decided syntactically: read vs write', `fact`='ACCESS_KIND', `status`='ambiguous'

Missing evidence:

- READ/WRITE distinction (level 1, DFG): access kind is a syntactic proxy here, so write-write and read-write cannot be separated from read-read, and a shared variable that never appears in an event-bearing expression (`counter++`) produces no event at all
- whether the two contexts really run concurrently: MAY_PARALLEL is undecidable from spawn points alone
- whether the lock actually guards this variable: holding a lock near an access is not the same as the lock protecting the data
- whether the variable tolerates weak consistency (a statistics counter may be allowed to race by design)

```cpp
            for (ggml_type type_b : {GGML_TYPE_F32}) {
                int m = dist_m(rng);
                int n = dist_n(rng);
                int k = dist_k(rng) * ggml_blck_size(type_a);
                test_cases.emplace_back(new test_mul_mat(type_a, type_b, m, n, k, { 1,  1}, {1, 1}));
```

```c
    UNUSED(ib);
    UNUSED(sumf);
    ggml_vec_dot_q5_0_q8_0_generic(n, s, bs, vx, bx, vy, by, nrc);
#endif
}
```

Verdict (recorded in `defect_review.json`):

- `verdict`: `REVIEWED`
- `is_true_positive`: `false` — **false positive**
- `failure_reason`: `identity`
- `needs_dfg`: `false`
- `missing_capability`: `声明身份与作用域（declaration identity / scope）`
- `confidence`: `0.9`
- `reason`: `` `n` 是 tests/test-backend-ops.cpp:10036 的局部量 `int n = dist_n(rng)`，另一侧是 ggml/src/ggml-cpu/arch/s390/quants.c:429 的形参 `int n`——两个不同的对象共用一个名字。 ``
- `annotator`: `AI 提议 + 人工确认（Claude Code 提议 / Kyber5323 逐条确认，2026-09-22）`
- `notes`: `null`

### DFV1-redis50-ea2342a8c8a0ae2f

- type: `1.1` / `race_condition`
- status: `ambiguous` (confidence 0.5)
- subject: `name` in `clusterManagerShowClusterInfo`
- candidate: `E-DEFECT-1c57f3cc7f782357d6770397`
- anchor: `event:183826c89453a9a5f7fdb9f774d67aad0d6788c5b9ba690e96719aad43f62497`
- source: `src/redis-cli.c:4860-4860`, `src/module.c:3774-3774`, `src/redis-cli.c:4859-4859`

Facts:

- `event`='DEREFERENCE', `matched_name`='name[8]', `method`='clusterManagerShowClusterInfo', `point`='event:183826c89453a9a5f7fdb9f774d67aad0d6788c5b9ba690e96719aad43f62497', `source`='src/redis-cli.c:4860-4860', `subject`='name', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='RM_GetClientUserNameById', `point`='event:9fd974a7970dbeba138ef76337cfbf446aa6a7ccdf3faa6809c347d8721fe4c5', `source`='src/module.c:3774-3774', `subject`='name', `subject_from`='assignment'
- `event`='CALL', `matched_name`='call_expression', `method`='clusterManagerShowClusterInfo', `point`='event:f7b252a228066ca58078ba9b8026ee1195f4320f6c8a85b2582d88d98f6e34f6', `source`='src/redis-cli.c:4859-4859', `subject`='name', `subject_from`='argument'

Uncertain facts:

- `decisive`=True, `detail`='the repository starts threads somewhere, so neither method is ordered', `fact`='MAY_PARALLEL', `status`='ambiguous'
- `decisive`=True, `detail`='no lock in this method', `fact`='PROTECTED_BY', `lock`='', `method`='clusterManagerShowClusterInfo', `status`='ambiguous'
- `decisive`=True, `detail`='no lock in this method', `fact`='PROTECTED_BY', `lock`='', `method`='RM_GetClientUserNameById', `status`='ambiguous'
- `decisive`=True, `detail`='write/read decided syntactically: read vs write', `fact`='ACCESS_KIND', `status`='ambiguous'

Missing evidence:

- READ/WRITE distinction (level 1, DFG): access kind is a syntactic proxy here, so write-write and read-write cannot be separated from read-read, and a shared variable that never appears in an event-bearing expression (`counter++`) produces no event at all
- whether the two contexts really run concurrently: MAY_PARALLEL is undecidable from spawn points alone
- whether the lock actually guards this variable: holding a lock near an access is not the same as the lock protecting the data
- whether the variable tolerates weak consistency (a statistics counter may be allowed to race by design)

```c
            char name[9];
            memcpy(name, node->name, 8);
            name[8] = '\0';
            listIter ri;
            listNode *rn;
```

```c
    }

    sds name = sdsnew(client->user->name);
    robj *str = createObject(OBJ_STRING, name);
    autoMemoryAdd(ctx, REDISMODULE_AM_STRING, str);
```

```c
            long long dbsize = -1;
            char name[9];
            memcpy(name, node->name, 8);
            name[8] = '\0';
            listIter ri;
```

Verdict (recorded in `defect_review.json`):

- `verdict`: `REVIEWED`
- `is_true_positive`: `false` — **false positive**
- `failure_reason`: `identity`
- `needs_dfg`: `false`
- `missing_capability`: `声明身份与作用域（declaration identity / scope）`
- `confidence`: `0.9`
- `reason`: `` `name` 是 src/redis-cli.c:4859-4860 的栈上局部 `char name[9]`，另一侧是 src/module.c:3774 的局部 `sds name`。 ``
- `annotator`: `AI 提议 + 人工确认（Claude Code 提议 / Kyber5323 逐条确认，2026-09-22）`
- `notes`: `null`

### DFV1-llamacpp100-0e498512e667d1c2

- type: `4.1` / `resource_lifetime`
- status: `ambiguous` (confidence 0.4)
- subject: `ctx` in `ggml_backend_hexagon_comm_init`
- candidate: `E-DEFECT-35da62c14bb19b347f40ccec`
- anchor: `event:98975bf99aed752daf7765f0d025dd02ddbdca42ebaaca0a29e7c4021ea8858a`
- source: `ggml/src/ggml-hexagon/ggml-hexagon.cpp:6985-6985`, `ggml/src/ggml-hexagon/ggml-hexagon.cpp:6986-6986`, `ggml/src/ggml-hexagon/ggml-hexagon.cpp:6987-6987`, `ggml/src/ggml-hexagon/ggml-hexagon.cpp:6990-6990`, `ggml/src/ggml-hexagon/ggml-hexagon.cpp:7009-7009`

Facts:

- `event`='ALLOC', `matched_name`='new_expression', `method`='ggml_backend_hexagon_comm_init', `point`='event:98975bf99aed752daf7765f0d025dd02ddbdca42ebaaca0a29e7c4021ea8858a', `source`='ggml/src/ggml-hexagon/ggml-hexagon.cpp:6985-6985', `subject`='ctx', `subject_from`='assignment'
- `event`='DEREFERENCE', `matched_name`='ctx->backends', `method`='ggml_backend_hexagon_comm_init', `point`='event:dcff24526032d5ff30032c3f8f311355f2779335e24731a666bdac377aac29f2', `source`='ggml/src/ggml-hexagon/ggml-hexagon.cpp:6986-6986', `subject`='ctx', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='ggml_backend_hexagon_comm_init', `point`='event:831e7a50d78fefd1c3774913a0bb0e06e7fbf258b3d460d784c0710797d973fa', `source`='ggml/src/ggml-hexagon/ggml-hexagon.cpp:6986-6986', `subject`='backends', `subject_from`='argument'
- `event`='DEREFERENCE', `matched_name`='ctx->n_backends', `method`='ggml_backend_hexagon_comm_init', `point`='event:b3c35f7d9b5875ec169c1d9248b3b7b42b884226e8edaec38dd31623d7dad35e', `source`='ggml/src/ggml-hexagon/ggml-hexagon.cpp:6987-6987', `subject`='ctx', `subject_from`=''
- `event`='BRANCH', `matched_name`='i<n_backends', `method`='ggml_backend_hexagon_comm_init', `point`='event:29ef61cacd7935656b57ce37e336cab7b97a7894c9a37dc5ad7419b1e0de70c3', `source`='ggml/src/ggml-hexagon/ggml-hexagon.cpp:6990-6990', `subject`='', `subject_from`=''
- `event`='RETURN', `matched_name`='return_statement', `method`='ggml_backend_hexagon_comm_init', `point`='event:c50b441984ff3d1468d171b34299ff1bd1e03ec4eac234da85d01988fc8df33a', `source`='ggml/src/ggml-hexagon/ggml-hexagon.cpp:7009-7009', `subject`='ctx', `subject_from`='return'

Uncertain facts:

- `decisive`=True, `detail`='the method returns this name, so ownership may leave with the caller', `fact`='RETURN_TRANSFER', `point`='event:c50b441984ff3d1468d171b34299ff1bd1e03ec4eac234da85d01988fc8df33a', `source`='ggml/src/ggml-hexagon/ggml-hexagon.cpp:7009-7009', `status`='ambiguous'

Missing evidence:

- the ownership contract of a callee: whether a function the resource is passed to takes ownership, borrows it, or frees it
- RAII and smart pointers: `unique_ptr<T> p(new T)` is an ALLOC with no RELEASE in the graph, so every owning smart pointer reads as a leak
- aliasing and reassignment: `q = p; free(q)`, `free(p); p = NULL` and `p = realloc(p, n)` are all invisible without a data-flow graph
- a release performed inside a callee: the graph only sees RELEASE points in the method it walked

```cpp
    }

    auto * ctx = new ggml_backend_hexagon_comm_context();
    ctx->backends.assign(backends, backends + n_backends);
    ctx->n_backends = n_backends;
```

```cpp

    auto * ctx = new ggml_backend_hexagon_comm_context();
    ctx->backends.assign(backends, backends + n_backends);
    ctx->n_backends = n_backends;

```

```cpp
    auto * ctx = new ggml_backend_hexagon_comm_context();
    ctx->backends.assign(backends, backends + n_backends);
    ctx->n_backends = n_backends;

    static ggml_hexagon_tensor_extra fence_extra { {}, 0, GGML_HEXAGON_TENSOR_FENCE };
```

```cpp

    static ggml_hexagon_tensor_extra fence_extra { {}, 0, GGML_HEXAGON_TENSOR_FENCE };
    for (size_t i = 0; i < n_backends; i++) {
        auto sess_i = static_cast<ggml_hexagon_session *>(backends[i]->context);
        ctx->fence_slots[i] = (volatile uint32_t *) sess_i->alloc_fence(1);
```

```cpp
    }

    return ctx;
}

```

Verdict (recorded in `defect_review.json`):

- `verdict`: `REVIEWED`
- `is_true_positive`: `false` — **false positive**
- `failure_reason`: `ownership`
- `needs_dfg`: `false`
- `missing_capability`: `所有权：返回值交给调用方（return transfer）`
- `confidence`: `0.9`
- `reason`: `` `ctx` 在 ggml/src/ggml-hexagon/ggml-hexagon.cpp:6985 分配后由 7009 `return ctx;` 交给调用方，`delete ctx` 在另一个函数 `ggml_backend_hexagon_comm_free`（7019）里。 ``
- `annotator`: `AI 提议 + 人工确认（Claude Code 提议 / Kyber5323 逐条确认，2026-09-22）`
- `notes`: `` 查询因为 `return ctx;` 自行降为 ambiguous——不确定性标注是对的。 ``

### DFV1-redis50-2f1d2d068a0cad93

- type: `1.4` / `lock_order`
- status: `ambiguous` (confidence 0.3)
- subject: `index` in `hnsw_acquire_read_slot`
- candidate: `E-DEFECT-f94d38c33d715ab3425af254`
- anchor: `event:928672935f3f214be6846c4bbfb1f15ccad41b19f4876bde8787f644ab3249ed`
- source: `modules/vector-sets/hnsw.c:2016-2016`, `modules/vector-sets/hnsw.c:2017-2017`

Facts:

- `holding_confirmed`=True, `lock`='index', `method`='hnsw_acquire_read_slot', `method_symbol_id`='symbol:406df0db7bc3af3e4c958ab16ab6d05c3cbf34ba272f0b42ef1f918064f074c6', `point`='event:928672935f3f214be6846c4bbfb1f15ccad41b19f4876bde8787f644ab3249ed', `role`='first', `side`='single', `source`='modules/vector-sets/hnsw.c:2016-2016'
- `holding_confirmed`=True, `lock`='index', `method`='hnsw_acquire_read_slot', `method_symbol_id`='symbol:406df0db7bc3af3e4c958ab16ab6d05c3cbf34ba272f0b42ef1f918064f074c6', `point`='event:18e2419f8f5f3cb56021c99442e9b1ac630cfc007f6d6bdf25af96942346f48c', `role`='second', `side`='single', `source`='modules/vector-sets/hnsw.c:2017-2017'

Uncertain facts:

- `decisive`=False, `detail`='a recursive mutex (PTHREAD_MUTEX_RECURSIVE) makes a second acquisition by the same thread legal', `fact`='MUTEX_RECURSIVE', `status`='ambiguous'
- `decisive`=True, `detail`="the lock's name is the root of a longer expression, so two different locks can carry the same name", `fact`='SUBJECT_PATH', `point`='event:928672935f3f214be6846c4bbfb1f15ccad41b19f4876bde8787f644ab3249ed', `source`='modules/vector-sets/hnsw.c:2016-2016', `status`='ambiguous'

Missing evidence:

- whether the mutex is recursive: under PTHREAD_MUTEX_RECURSIVE a second acquisition by the same thread is legal, and the graph does not hold the mutex's type
- the execution context: whether the two methods can run concurrently at all -- nothing in the graph orders them
- lock aliasing: two expressions naming the same mutex (`&s->m` and `s->m`) are two names here, and one name is one lock
- the failure path of a non-blocking acquisition: a try-lock that fails leaves the lock unheld and the graph cannot tell the two arms apart

```c
    /* First try a non-blocking approach on all slots. */
    for (uint32_t i = 0; i < HNSW_MAX_THREADS; i++) {
        if (pthread_mutex_trylock(&index->slot_locks[i]) == 0) {
            if (pthread_rwlock_rdlock(&index->global_lock) != 0) {
                pthread_mutex_unlock(&index->slot_locks[i]);
```

```c
    for (uint32_t i = 0; i < HNSW_MAX_THREADS; i++) {
        if (pthread_mutex_trylock(&index->slot_locks[i]) == 0) {
            if (pthread_rwlock_rdlock(&index->global_lock) != 0) {
                pthread_mutex_unlock(&index->slot_locks[i]);
                return -1;
```

Verdict (recorded in `defect_review.json`):

- `verdict`: `REVIEWED`
- `is_true_positive`: `false` — **false positive**
- `failure_reason`: `identity`
- `needs_dfg`: `false`
- `missing_capability`: `锁身份：subject_exact=false（根名不是那个对象）`
- `confidence`: `0.95`
- `reason`: `` modules/vector-sets/hnsw.c:2016 `pthread_mutex_trylock(&index->slot_locks[i])` 与 2017 `pthread_rwlock_rdlock(&index->global_lock)` 是两把不同的锁，根名都叫 `index`。 ``
- `annotator`: `AI 提议 + 人工确认（Claude Code 提议 / Kyber5323 逐条确认，2026-09-22）`
- `notes`: `null`

### DFV1-llamacpp100-99f1097a8f14d47e

- type: `4.1` / `resource_lifetime`
- status: `resolved` (confidence 0.6)
- subject: `image_tokens` in `mtmd_test_create_input_chunks`
- candidate: `E-DEFECT-645a424748a34abbd71a9127`
- anchor: `event:1a6952fe7f8cb4fa4740fd3cef85907dd0baec2e4ef2292df296ad3363e1fe03`
- source: `tools/mtmd/mtmd.cpp:2559-2559`, `tools/mtmd/mtmd.cpp:2560-2560`, `tools/mtmd/mtmd.cpp:2561-2561`, `tools/mtmd/mtmd.cpp:2562-2562`, `tools/mtmd/mtmd.cpp:2563-2563`, `tools/mtmd/mtmd.cpp:2567-2567`, `tools/mtmd/mtmd.cpp:2570-2570`, `tools/mtmd/mtmd.cpp:2572-2572`

Facts:

- `event`='ALLOC', `matched_name`='new_expression', `method`='mtmd_test_create_input_chunks', `point`='event:1a6952fe7f8cb4fa4740fd3cef85907dd0baec2e4ef2292df296ad3363e1fe03', `source`='tools/mtmd/mtmd.cpp:2559-2559', `subject`='image_tokens', `subject_from`='assignment'
- `event`='DEREFERENCE', `matched_name`='image_tokens->nx', `method`='mtmd_test_create_input_chunks', `point`='event:75baacbc785a8791cf723dca4f59f758695a34309acd91cfbfb50d552b8b805a', `source`='tools/mtmd/mtmd.cpp:2560-2560', `subject`='image_tokens', `subject_from`=''
- `event`='DEREFERENCE', `matched_name`='image_tokens->ny', `method`='mtmd_test_create_input_chunks', `point`='event:2593e58cf44134e06be7554544c1716015a85cbe21e71cdfd4755c48043db2e0', `source`='tools/mtmd/mtmd.cpp:2561-2561', `subject`='image_tokens', `subject_from`=''
- `event`='DEREFERENCE', `matched_name`='image_tokens->batch_f32', `method`='mtmd_test_create_input_chunks', `point`='event:91477ccad11b518659f3400a1576b289c4ed2759a01baf11f8c84703c7920285', `source`='tools/mtmd/mtmd.cpp:2562-2562', `subject`='image_tokens', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='mtmd_test_create_input_chunks', `point`='event:7de329b46f3b11995bbd5be3237cd8578caebfdde9b4a8463faa8e41eb58ffcb', `source`='tools/mtmd/mtmd.cpp:2562-2562', `subject`='', `subject_from`=''
- `event`='DEREFERENCE', `matched_name`='image_tokens->id', `method`='mtmd_test_create_input_chunks', `point`='event:5e7324a80d9dc802429064e390791c0c9086408e244378e5e6844405c1d85998', `source`='tools/mtmd/mtmd.cpp:2563-2563', `subject`='image_tokens', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='mtmd_test_create_input_chunks', `point`='event:e1964e9f83005a318a1b6a2ffec72e85888316a6af9c34e45fe8ea26dce4f9a2', `source`='tools/mtmd/mtmd.cpp:2567-2567', `subject`='chunk_image', `subject_from`='assignment'
- `event`='DEREFERENCE', `matched_name`='chunks->entries', `method`='mtmd_test_create_input_chunks', `point`='event:3e7caba1ee82f200e93776783211b7b70cb709d24b1a012596ab7f41b8c89a18', `source`='tools/mtmd/mtmd.cpp:2570-2570', `subject`='chunks', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='mtmd_test_create_input_chunks', `point`='event:aebce46e08efa3f03f9a4ff55c914b0cab9964a819d1b19cd3550565abc887d0', `source`='tools/mtmd/mtmd.cpp:2570-2570', `subject`='chunk_image', `subject_from`='argument'
- `event`='CALL', `matched_name`='call_expression', `method`='mtmd_test_create_input_chunks', `point`='event:33331ce99a6db0e842ffaa623f38de382a2a85a910c82eb61412787944da9d73', `source`='tools/mtmd/mtmd.cpp:2570-2570', `subject`='', `subject_from`=''
- `event`='RETURN', `matched_name`='return_statement', `method`='mtmd_test_create_input_chunks', `point`='event:b4d4be95ef7db7a008d8e98299065364c824cebc994c30f897435fe8803255d1', `source`='tools/mtmd/mtmd.cpp:2572-2572', `subject`='chunks', `subject_from`='return'

Uncertain facts:


Missing evidence:

- the ownership contract of a callee: whether a function the resource is passed to takes ownership, borrows it, or frees it
- RAII and smart pointers: `unique_ptr<T> p(new T)` is an ALLOC with no RELEASE in the graph, so every owning smart pointer reads as a leak
- aliasing and reassignment: `q = p; free(q)`, `free(p); p = NULL` and `p = realloc(p, n)` are all invisible without a data-flow graph
- a release performed inside a callee: the graph only sees RELEASE points in the method it walked

```cpp

    // create an image chunk
    mtmd_image_tokens_ptr image_tokens(new mtmd_image_tokens);
    image_tokens->nx = 4;
    image_tokens->ny = 4;
```

```cpp
    // create an image chunk
    mtmd_image_tokens_ptr image_tokens(new mtmd_image_tokens);
    image_tokens->nx = 4;
    image_tokens->ny = 4;
    image_tokens->batch_f32.entries.resize(16);
```

```cpp
    mtmd_image_tokens_ptr image_tokens(new mtmd_image_tokens);
    image_tokens->nx = 4;
    image_tokens->ny = 4;
    image_tokens->batch_f32.entries.resize(16);
    image_tokens->id = "image_1";
```

```cpp
    image_tokens->nx = 4;
    image_tokens->ny = 4;
    image_tokens->batch_f32.entries.resize(16);
    image_tokens->id = "image_1";
    mtmd_input_chunk chunk_image{
```

```cpp
    image_tokens->ny = 4;
    image_tokens->batch_f32.entries.resize(16);
    image_tokens->id = "image_1";
    mtmd_input_chunk chunk_image{
        MTMD_INPUT_CHUNK_TYPE_IMAGE,
```

```cpp
        MTMD_INPUT_CHUNK_TYPE_IMAGE,
        {}, // text tokens
        std::move(image_tokens),
        nullptr, // audio tokens
    };
```

```cpp
        nullptr, // audio tokens
    };
    chunks->entries.emplace_back(std::move(chunk_image));

    return chunks;
```

```cpp
    chunks->entries.emplace_back(std::move(chunk_image));

    return chunks;
}

```

Verdict (recorded in `defect_review.json`):

- `verdict`: `REVIEWED`
- `is_true_positive`: `false` — **false positive**
- `failure_reason`: `ownership`
- `needs_dfg`: `false`
- `missing_capability`: `RAII：智能指针持有（unique_ptr）`
- `confidence`: `0.95`
- `reason`: `` `mtmd_image_tokens_ptr` 是 `std::unique_ptr`（tools/mtmd/mtmd.cpp:286），2559 的分配被它接住，2568 `std::move(image_tokens)` 进 chunk。 ``
- `annotator`: `AI 提议 + 人工确认（Claude Code 提议 / Kyber5323 逐条确认，2026-09-22）`
- `notes`: `null`

### DFV1-redis50-0af0826a76bbb393

- type: `4.1` / `resource_lifetime`
- status: `ambiguous` (confidence 0.4)
- subject: `SPT` in `spt_init`
- candidate: `E-DEFECT-37f6602a1d51bcb786c68158`
- anchor: `event:616b3560028782830a3ea7b8c76d706c2d5c353d53b47d0a5409273a2a41bb77`
- source: `src/setproctitle.c:232-232`, `src/setproctitle.c:236-236`, `src/setproctitle.c:241-241`, `src/setproctitle.c:246-246`, `src/setproctitle.c:249-249`, `src/setproctitle.c:253-253`, `src/setproctitle.c:256-256`, `src/setproctitle.c:263-263`

Facts:

- `event`='ALLOC', `matched_name`='strdup', `method`='spt_init', `point`='event:616b3560028782830a3ea7b8c76d706c2d5c353d53b47d0a5409273a2a41bb77', `source`='src/setproctitle.c:232-232', `subject`='SPT', `subject_from`='assignment'
- `event`='CALL', `matched_name`='call_expression', `method`='spt_init', `point`='event:aa101cdb48d1b5bf97d8d97a58baac1201a56fbe5c58b1276a149b350f7bd476', `source`='src/setproctitle.c:232-232', `subject`='SPT', `subject_from`='assignment'
- `event`='CHECK', `matched_name`='!(SPT.arg0=strdup(argv[0]))', `method`='spt_init', `point`='event:eecd155d2acb268130ef83b355795b8b5a9bf5b6d77fa213872c27d815e85fa1', `source`='src/setproctitle.c:232-232', `subject`='', `subject_from`=''
- `event`='ALLOC', `matched_name`='strdup', `method`='spt_init', `point`='event:46c2439db3422541dccdf886557b85a3fcb1615ca559d33b00f5de1a9e25bbdd', `source`='src/setproctitle.c:236-236', `subject`='tmp', `subject_from`='assignment'
- `event`='CALL', `matched_name`='call_expression', `method`='spt_init', `point`='event:bbbe12219fade7c4fda7fb052f14e279cea7884758a335769b09a2fdf59d087d', `source`='src/setproctitle.c:236-236', `subject`='tmp', `subject_from`='assignment'
- `event`='CHECK', `matched_name`='!(tmp=strdup(program_invocation_name))', `method`='spt_init', `point`='event:a7051549b89e049b94c03c9011a4898e338e05f6adb46d45fefcd46385d9f9e9', `source`='src/setproctitle.c:236-236', `subject`='', `subject_from`=''
- `event`='ALLOC', `matched_name`='strdup', `method`='spt_init', `point`='event:64ee9372f711e6c2c7aa1efe56eddd09c16141af7e3112b3d05ecba06a225a10', `source`='src/setproctitle.c:241-241', `subject`='tmp', `subject_from`='assignment'
- `event`='CALL', `matched_name`='call_expression', `method`='spt_init', `point`='event:78395f4387daf72c18691f50eef7808600f8908b02df2493f08b8557ecb17ba5', `source`='src/setproctitle.c:241-241', `subject`='tmp', `subject_from`='assignment'
- `event`='CHECK', `matched_name`='!(tmp=strdup(program_invocation_short_name))', `method`='spt_init', `point`='event:240b6f4a8c2ec856b44d8f2b509224eb840d58f4955c347c8a2e071155e44b60', `source`='src/setproctitle.c:241-241', `subject`='', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='spt_init', `point`='event:49f22c8c2887f2d897c455c21e93a3f7bd5e44430b22eee0cbbe99333ce25e7f', `source`='src/setproctitle.c:246-246', `subject`='tmp', `subject_from`='assignment'
- `event`='ALLOC', `matched_name`='strdup', `method`='spt_init', `point`='event:2c40b1d2f5af7fbecb9e9729e132cbd1364dfdd13cd4b3bc6bdb16c55cc36642', `source`='src/setproctitle.c:246-246', `subject`='tmp', `subject_from`='assignment'
- `event`='CALL', `matched_name`='call_expression', `method`='spt_init', `point`='event:f8b528550ff4f5e96132e1a63c80afaafc4055c73452e344ea6d39e7ea2dba3c', `source`='src/setproctitle.c:246-246', `subject`='tmp', `subject_from`='assignment'
- `event`='CHECK', `matched_name`='!(tmp=strdup(getprogname()))', `method`='spt_init', `point`='event:6f7e27170785fc9c5e900d3adce4c02b4b6f1ebf60598d52dca5028e435b29d0', `source`='src/setproctitle.c:246-246', `subject`='', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='spt_init', `point`='event:2c95509c4395a5768621ddeeaaf00e71ee772666a16a7e69bea38f03309e9f81', `source`='src/setproctitle.c:249-249', `subject`='tmp', `subject_from`='argument'
- `event`='CALL', `matched_name`='call_expression', `method`='spt_init', `point`='event:705bf055ab5d7d832052e8cf90781af22215718a4d2a4cbf1ad4ea84736996e6', `source`='src/setproctitle.c:253-253', `subject`='error', `subject_from`='assignment'
- `event`='BRANCH', `matched_name`='(error=spt_copyenv(envc,envp))', `method`='spt_init', `point`='event:238232cb05bcfdb3c1e8bb160541da2246eda396571f8246a1c44dd5ab9fa282', `source`='src/setproctitle.c:253-253', `subject`='', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='spt_init', `point`='event:a4d2366c20d4cadee8490e8a22277c74066078fde5e5c2de868cba297c093b9e', `source`='src/setproctitle.c:256-256', `subject`='error', `subject_from`='assignment'
- `event`='BRANCH', `matched_name`='(error=spt_copyargs(argc,argv))', `method`='spt_init', `point`='event:8ce857042a848274c191faabe577cb2c402aee2f6458c8069e10fcbd0aa9cc76', `source`='src/setproctitle.c:256-256', `subject`='', `subject_from`=''
- `event`='RETURN', `matched_name`='return_statement', `method`='spt_init', `point`='event:ae56f0a8469d2ecebb057ba9190bd886a54ed09cfa3ecc866f5bc4d54a5b11f8', `source`='src/setproctitle.c:263-263', `subject`='', `subject_from`=''

Uncertain facts:

- `decisive`=True, `detail`='the name is the root of a longer expression (`s` in `s->buf`), so the same name elsewhere may be a different object', `fact`='SUBJECT_PATH', `point`='event:616b3560028782830a3ea7b8c76d706c2d5c353d53b47d0a5409273a2a41bb77', `source`='src/setproctitle.c:232-232', `status`='ambiguous'

Missing evidence:

- the ownership contract of a callee: whether a function the resource is passed to takes ownership, borrows it, or frees it
- RAII and smart pointers: `unique_ptr<T> p(new T)` is an ALLOC with no RELEASE in the graph, so every owning smart pointer reads as a leak
- aliasing and reassignment: `q = p; free(q)`, `free(p); p = NULL` and `p = realloc(p, n)` are all invisible without a data-flow graph
- a release performed inside a callee: the graph only sees RELEASE points in the method it walked

```c
	 * to keep the original value elsewhere.
	 */
	if (!(SPT.arg0 = strdup(argv[0])))
		goto syerr;

```

```c

#if __linux__
	if (!(tmp = strdup(program_invocation_name)))
		goto syerr;

```

```c
	program_invocation_name = tmp;

	if (!(tmp = strdup(program_invocation_short_name)))
		goto syerr;

```

```c
	program_invocation_short_name = tmp;
#elif __APPLE__
	if (!(tmp = strdup(getprogname())))
		goto syerr;

```

```c
		goto syerr;

	setprogname(tmp);
#endif

```

```c

    /* Now make a full deep copy of the environment and argv[] */
	if ((error = spt_copyenv(envc, envp)))
		goto error;

```

```c
		goto error;

	if ((error = spt_copyargs(argc, argv)))
		goto error;

```

```c
	SPT.end  = end;

	return;
syerr:
	error = errno;
```

Verdict (recorded in `defect_review.json`):

- `verdict`: `REVIEWED`
- `is_true_positive`: `false` — **false positive**
- `failure_reason`: `ownership`
- `needs_dfg`: `false`
- `missing_capability`: `存储期：文件作用域 static`
- `confidence`: `0.85`
- `reason`: `` `SPT.arg0 = strdup(argv[0])`（src/setproctitle.c:232），`SPT` 是文件作用域 static 结构，进程存活期内故意持有。 ``
- `annotator`: `AI 提议 + 人工确认（Claude Code 提议 / Kyber5323 逐条确认，2026-09-22）`
- `notes`: `判 FP 的依据是「设计如此」（进程/文件作用域）；若认为意图类判断不该进分母，应填 uncertain。`

### DFV1-llamacpp100-10ec805fa54530ba

- type: `4.1` / `resource_lifetime`
- status: `unresolved` (confidence 0.2)
- subject: `` in `make_test_cases_eval`
- candidate: `E-DEFECT-9d4a0c8b70eb444ab3ee0cda`
- anchor: `event:87be2abea719f4543284840bb90f11b8a300f3327f1c54cef84640fe778fb842`
- source: `tests/test-backend-ops.cpp:9177-9177`, `tests/test-backend-ops.cpp:9178-9178`, `tests/test-backend-ops.cpp:9189-9189`, `tests/test-backend-ops.cpp:9190-9190`, `tests/test-backend-ops.cpp:9191-9191`, `tests/test-backend-ops.cpp:9192-9192`, `tests/test-backend-ops.cpp:9193-9193`, `tests/test-backend-ops.cpp:9210-9210`, `tests/test-backend-ops.cpp:9211-9211`, `tests/test-backend-ops.cpp:9212-9212`, `tests/test-backend-ops.cpp:9213-9213`, `tests/test-backend-ops.cpp:9214-9214`, `tests/test-backend-ops.cpp:9215-9215`, `tests/test-backend-ops.cpp:9216-9216`, `tests/test-backend-ops.cpp:9217-9217`, `tests/test-backend-ops.cpp:9218-9218`, `tests/test-backend-ops.cpp:9219-9219`, `tests/test-backend-ops.cpp:9220-9220`, `tests/test-backend-ops.cpp:9221-9221`, `tests/test-backend-ops.cpp:9224-9224`, `tests/test-backend-ops.cpp:9225-9225`, `tests/test-backend-ops.cpp:9226-9226`, `tests/test-backend-ops.cpp:9227-9227`, `tests/test-backend-ops.cpp:9286-9286`, `tests/test-backend-ops.cpp:9302-9302`

Facts:

- `event`='ALLOC', `matched_name`='new_expression', `method`='make_test_cases_eval', `point`='event:87be2abea719f4543284840bb90f11b8a300f3327f1c54cef84640fe778fb842', `source`='tests/test-backend-ops.cpp:9177-9177', `subject`='', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='make_test_cases_eval', `point`='event:bf3dffb8536d299baf5dfcf71ae5e01323fe4f3ec1e927068336097f222a9291', `source`='tests/test-backend-ops.cpp:9177-9177', `subject`='', `subject_from`=''
- `event`='BRANCH', `matched_name`='{1,3}', `method`='make_test_cases_eval', `point`='event:75d1f691d98f5b6f896b8cedc6b60012327b171613a01c5abdf76e0c2c5dc654', `source`='tests/test-backend-ops.cpp:9178-9178', `subject`='', `subject_from`=''
- `event`='ALLOC', `matched_name`='new_expression', `method`='make_test_cases_eval', `point`='event:34f8115b8081f8290cc193983510ed8d46b80eaf38b1be30ebaeff6761c1d20d', `source`='tests/test-backend-ops.cpp:9189-9189', `subject`='', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='make_test_cases_eval', `point`='event:e5bc3e7b6d9b5527ad031b6ac6c01740048772b1b5f74dd7615b6e03a39fa836', `source`='tests/test-backend-ops.cpp:9189-9189', `subject`='', `subject_from`=''
- `event`='ALLOC', `matched_name`='new_expression', `method`='make_test_cases_eval', `point`='event:a99c33ae7745f28cda32f9072ad106c1d73f3a990ddcbc58e9752a2c22ee5db1', `source`='tests/test-backend-ops.cpp:9190-9190', `subject`='', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='make_test_cases_eval', `point`='event:37e6b38c2da23d8c70436287fe4572d56395a4db44fc75b22179ddb43ad969a0', `source`='tests/test-backend-ops.cpp:9190-9190', `subject`='', `subject_from`=''
- `event`='ALLOC', `matched_name`='new_expression', `method`='make_test_cases_eval', `point`='event:245b804836e0b6d05d51590b1a483c9c7e396f340c47fbb963a753e51d7a32a6', `source`='tests/test-backend-ops.cpp:9191-9191', `subject`='', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='make_test_cases_eval', `point`='event:170f8ddf737c0c8edb5473ed2a72963f2b7a81ba6a41cf46bc8cb86115956cdf', `source`='tests/test-backend-ops.cpp:9191-9191', `subject`='', `subject_from`=''
- `event`='ALLOC', `matched_name`='new_expression', `method`='make_test_cases_eval', `point`='event:f50db2cb58756e71bbdc69c545b710840a51db9adcbd03c0f99503ab3252693b', `source`='tests/test-backend-ops.cpp:9192-9192', `subject`='', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='make_test_cases_eval', `point`='event:90619190f67fd6039ae7199166e47a8d688d308a70da17eca1eb7154b26a1993', `source`='tests/test-backend-ops.cpp:9192-9192', `subject`='', `subject_from`=''
- `event`='BRANCH', `matched_name`='{1,3}', `method`='make_test_cases_eval', `point`='event:059057715cfb64acd5878997872e91aac6afb382978c98a5e22caa7c59a13433', `source`='tests/test-backend-ops.cpp:9193-9193', `subject`='', `subject_from`=''
- `event`='ALLOC', `matched_name`='new_expression', `method`='make_test_cases_eval', `point`='event:67f948631bd04918263aa0425854854678c8405a98ae2cd7dfd868110a1e8348', `source`='tests/test-backend-ops.cpp:9210-9210', `subject`='', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='make_test_cases_eval', `point`='event:2618201be19b840fb458b79433c2ff9c73c7b084815614119ac010be9a9902eb', `source`='tests/test-backend-ops.cpp:9210-9210', `subject`='', `subject_from`=''
- `event`='ALLOC', `matched_name`='new_expression', `method`='make_test_cases_eval', `point`='event:d0de43df185876ea3ce61df6e408d179ef695e9de9a2da05fe9afd08a40e44a8', `source`='tests/test-backend-ops.cpp:9211-9211', `subject`='', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='make_test_cases_eval', `point`='event:a6416fe0cf648bcf4dcbf6ec6aeadc07e89edde6f3fafc91534bf7a18d2d74e8', `source`='tests/test-backend-ops.cpp:9211-9211', `subject`='', `subject_from`=''
- `event`='ALLOC', `matched_name`='new_expression', `method`='make_test_cases_eval', `point`='event:2db5d7a4b33b6bfddbf6687a3c1e9ca81ed9bcd87bd1f4c25c6eb7a066db88bc', `source`='tests/test-backend-ops.cpp:9212-9212', `subject`='', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='make_test_cases_eval', `point`='event:8afd375dbe4eb1273e530c53da148930e8d62cf3cf1fd6379c43aa805d4cff48', `source`='tests/test-backend-ops.cpp:9212-9212', `subject`='', `subject_from`=''
- `event`='ALLOC', `matched_name`='new_expression', `method`='make_test_cases_eval', `point`='event:01b1221cc44fb9fda2d970bb0011ceaeeaaf67abb1870a5220fc89e8756ce2b3', `source`='tests/test-backend-ops.cpp:9213-9213', `subject`='', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='make_test_cases_eval', `point`='event:1b9a8b151349b9712f85a1cf3b4a58b96dda7a3c55d64c696f77816779d26f7a', `source`='tests/test-backend-ops.cpp:9213-9213', `subject`='', `subject_from`=''
- `event`='ALLOC', `matched_name`='new_expression', `method`='make_test_cases_eval', `point`='event:fc62e85d9a3589a6e0f198ba581ff0ef6d20dc5660a44021c59bccf03de09114', `source`='tests/test-backend-ops.cpp:9214-9214', `subject`='', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='make_test_cases_eval', `point`='event:f76fd6a6558d2cde6dcfbd5d9d7a97c0a038c361992b116a0e37ebf0dc398158', `source`='tests/test-backend-ops.cpp:9214-9214', `subject`='', `subject_from`=''
- `event`='ALLOC', `matched_name`='new_expression', `method`='make_test_cases_eval', `point`='event:329d83145c34adf5293b231f8b54729c44a37c8538ebb77776d30135a378293d', `source`='tests/test-backend-ops.cpp:9215-9215', `subject`='', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='make_test_cases_eval', `point`='event:90db2f997fb164e8aa8fc7c64d0c475641bce56d6a70a4b808747d0741a31660', `source`='tests/test-backend-ops.cpp:9215-9215', `subject`='', `subject_from`=''
- `event`='ALLOC', `matched_name`='new_expression', `method`='make_test_cases_eval', `point`='event:8fc02121050b55e06d3fba3dba88b67cf27ac47b5d6aacf45b470b4628e7384b', `source`='tests/test-backend-ops.cpp:9216-9216', `subject`='', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='make_test_cases_eval', `point`='event:88ff93031e6cd60d1ce262b47fa48dcf26fda6a4a7139976b87531f2f935bb0f', `source`='tests/test-backend-ops.cpp:9216-9216', `subject`='', `subject_from`=''
- `event`='ALLOC', `matched_name`='new_expression', `method`='make_test_cases_eval', `point`='event:0e7b6100c63b9e4438207a8c2723ee6edf5896d8631bec31eadc72e76bbf64cd', `source`='tests/test-backend-ops.cpp:9217-9217', `subject`='', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='make_test_cases_eval', `point`='event:5bbe019e3c6b61f0213f30d909a1012c8de2e9b1237c8219724bb14b49333672', `source`='tests/test-backend-ops.cpp:9217-9217', `subject`='', `subject_from`=''
- `event`='ALLOC', `matched_name`='new_expression', `method`='make_test_cases_eval', `point`='event:2e6dabb21c918996432804f80aaca55ccac9be1248f89166cc031568e3c41e3b', `source`='tests/test-backend-ops.cpp:9218-9218', `subject`='', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='make_test_cases_eval', `point`='event:9e6321cdf89b88670c3f1fc9d6831924622c5acf5786dfda2a24e2518f3b0af6', `source`='tests/test-backend-ops.cpp:9218-9218', `subject`='', `subject_from`=''
- `event`='ALLOC', `matched_name`='new_expression', `method`='make_test_cases_eval', `point`='event:1ee39737e8897a9d47eed1b65cfdea853326983e0b555556ce39254c51fbb56b', `source`='tests/test-backend-ops.cpp:9219-9219', `subject`='', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='make_test_cases_eval', `point`='event:b918431ecd13ac9959a652636a33fa24741f359c666a8867cbffdd23844f02f6', `source`='tests/test-backend-ops.cpp:9219-9219', `subject`='', `subject_from`=''
- `event`='ALLOC', `matched_name`='new_expression', `method`='make_test_cases_eval', `point`='event:15cb8ed85793e2111f7d715a572f9c1a95b7501d4f79285966c201a3edea521d', `source`='tests/test-backend-ops.cpp:9220-9220', `subject`='', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='make_test_cases_eval', `point`='event:3874267def74b168a9d334e62e454ea744d7fffe68df478b8519aacd54b8f9e1', `source`='tests/test-backend-ops.cpp:9220-9220', `subject`='', `subject_from`=''
- `event`='ALLOC', `matched_name`='new_expression', `method`='make_test_cases_eval', `point`='event:e8965edb57bb87329bde58cf24dc9985c96e5a522408464863ff4f622628e2eb', `source`='tests/test-backend-ops.cpp:9221-9221', `subject`='', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='make_test_cases_eval', `point`='event:5985283cab48a6c6322e244c80c79474a3ffadefcafc63f65afdce9039e64d3c', `source`='tests/test-backend-ops.cpp:9221-9221', `subject`='', `subject_from`=''
- `event`='ALLOC', `matched_name`='new_expression', `method`='make_test_cases_eval', `point`='event:26bfb8d49e40783148e298bf285b64e9fb7326b0efca7feed32aa151f687f658', `source`='tests/test-backend-ops.cpp:9224-9224', `subject`='', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='make_test_cases_eval', `point`='event:828df5d829500879ed91c3dfa9345ecd8adac369e1afe0518df39fa4243d1283', `source`='tests/test-backend-ops.cpp:9224-9224', `subject`='', `subject_from`=''
- `event`='ALLOC', `matched_name`='new_expression', `method`='make_test_cases_eval', `point`='event:44fa49ef394eb2f4f082b92a92964a71e9d6e7028b03f52dea7fb7a6781bd9af', `source`='tests/test-backend-ops.cpp:9225-9225', `subject`='', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='make_test_cases_eval', `point`='event:f4507bab1d2ded7d7a28c42b6a032aea5afec9660a578bd1ae27046280cce3c4', `source`='tests/test-backend-ops.cpp:9225-9225', `subject`='', `subject_from`=''
- `event`='ALLOC', `matched_name`='new_expression', `method`='make_test_cases_eval', `point`='event:7da5bfeafa31aaf5267ae93e59887bce977fb2da50eff7f357ac47a49227fe7c', `source`='tests/test-backend-ops.cpp:9226-9226', `subject`='', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='make_test_cases_eval', `point`='event:303ea0b7cfcd1eeeb717bed2f1a504094957c8f02d4b3e8444d57c6c370e3518', `source`='tests/test-backend-ops.cpp:9226-9226', `subject`='', `subject_from`=''
- `event`='BRANCH', `matched_name`='{1,3}', `method`='make_test_cases_eval', `point`='event:060112a7f5b7346e783714269410def18d3f61fc9987c103a054b25ae8c57a7b', `source`='tests/test-backend-ops.cpp:9227-9227', `subject`='', `subject_from`=''
- `event`='BRANCH', `matched_name`='{GGML_TYPE_F32,GGML_TYPE_F16}', `method`='make_test_cases_eval', `point`='event:ee3127602c7b20d903b320957a9ba12a56f78469c734290a96ae62fd164af30b', `source`='tests/test-backend-ops.cpp:9286-9286', `subject`='', `subject_from`=''
- `event`='RETURN', `matched_name`='return_statement', `method`='make_test_cases_eval', `point`='event:73431ab45edd456917b19aeb6d12f7c4a8b22ae8c95787a7719c5aaafc1cf362', `source`='tests/test-backend-ops.cpp:9302-9302', `subject`='', `subject_from`=''

Uncertain facts:

- `decisive`=True, `detail`='the acquisition names no target, so the candidate cannot be attributed', `fact`='SUBJECT_UNRESOLVED', `status`='ambiguous'

Missing evidence:

- the ownership contract of a callee: whether a function the resource is passed to takes ownership, borrows it, or frees it
- RAII and smart pointers: `unique_ptr<T> p(new T)` is an ALLOC with no RELEASE in the graph, so every owning smart pointer reads as a leak
- aliasing and reassignment: `q = p; free(q)`, `free(p); p = NULL` and `p = realloc(p, n)` are all invisible without a data-flow graph
- a release performed inside a callee: the graph only sees RELEASE points in the method it walked

```cpp
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F32, {3000, 128, 1, 1}, {3, 128, 1280, 1}, 1, 0, 1, 0, 1, 0, false));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16, {3000, 128, 1, 1}, {3, 128, 1280, 1}, 1, 0, 1, 0, 1, 0, false));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16, {3000, 384, 1, 1}, {3, 384,  384, 1}, 1, 0, 1, 0, 1, 0, false));
    for (int s0 : {1, 3}) {
        for (int p0 : {0, 3}) {
```

```cpp
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16, {3000, 128, 1, 1}, {3, 128, 1280, 1}, 1, 0, 1, 0, 1, 0, false));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16, {3000, 384, 1, 1}, {3, 384,  384, 1}, 1, 0, 1, 0, 1, 0, false));
    for (int s0 : {1, 3}) {
        for (int p0 : {0, 3}) {
            for (int d0 : {1, 3}) {
```

```cpp

    // im2col 2D
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F32, GGML_TYPE_F32));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F32, GGML_TYPE_F16));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F32));
```

```cpp
    // im2col 2D
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F32, GGML_TYPE_F32));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F32, GGML_TYPE_F16));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F32));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16));
```

```cpp
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F32, GGML_TYPE_F32));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F32, GGML_TYPE_F16));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F32));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16));
    for (int s0 : {1, 3}) {
```

```cpp
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F32, GGML_TYPE_F16));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F32));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16));
    for (int s0 : {1, 3}) {
        for (int s1 : {1, 3}) {
```

```cpp
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F32));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16));
    for (int s0 : {1, 3}) {
        for (int s1 : {1, 3}) {
            for (int p0 : {0, 3}) {
```

```cpp

    // extra tests for im2col 2D
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16, {12, 12, 1, 32}, {3, 3, 1, 32}, 1, 1, 1, 1, 1, 1, true));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16, {12, 12, 2, 32}, {3, 3, 2, 32}, 1, 1, 1, 1, 1, 1, true));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16, {12, 12, 1, 1024}, {3, 3, 1, 1024}, 1, 1, 1, 1, 1, 1, true));
```

```cpp
    // extra tests for im2col 2D
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16, {12, 12, 1, 32}, {3, 3, 1, 32}, 1, 1, 1, 1, 1, 1, true));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16, {12, 12, 2, 32}, {3, 3, 2, 32}, 1, 1, 1, 1, 1, 1, true));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16, {12, 12, 1, 1024}, {3, 3, 1, 1024}, 1, 1, 1, 1, 1, 1, true));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16, {12, 12, 2, 1024}, {3, 3, 2, 1024}, 1, 1, 1, 1, 1, 1, true));
```

```cpp
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16, {12, 12, 1, 32}, {3, 3, 1, 32}, 1, 1, 1, 1, 1, 1, true));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16, {12, 12, 2, 32}, {3, 3, 2, 32}, 1, 1, 1, 1, 1, 1, true));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16, {12, 12, 1, 1024}, {3, 3, 1, 1024}, 1, 1, 1, 1, 1, 1, true));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16, {12, 12, 2, 1024}, {3, 3, 2, 1024}, 1, 1, 1, 1, 1, 1, true));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16, {12, 12, 1, 2048}, {3, 3, 1, 2048}, 1, 1, 1, 1, 1, 1, true));
```

```cpp
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16, {12, 12, 2, 32}, {3, 3, 2, 32}, 1, 1, 1, 1, 1, 1, true));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16, {12, 12, 1, 1024}, {3, 3, 1, 1024}, 1, 1, 1, 1, 1, 1, true));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16, {12, 12, 2, 1024}, {3, 3, 2, 1024}, 1, 1, 1, 1, 1, 1, true));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16, {12, 12, 1, 2048}, {3, 3, 1, 2048}, 1, 1, 1, 1, 1, 1, true));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16, {12, 12, 2, 2048}, {3, 3, 2, 2048}, 1, 1, 1, 1, 1, 1, true));
```

```cpp
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16, {12, 12, 1, 1024}, {3, 3, 1, 1024}, 1, 1, 1, 1, 1, 1, true));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16, {12, 12, 2, 1024}, {3, 3, 2, 1024}, 1, 1, 1, 1, 1, 1, true));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16, {12, 12, 1, 2048}, {3, 3, 1, 2048}, 1, 1, 1, 1, 1, 1, true));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16, {12, 12, 2, 2048}, {3, 3, 2, 2048}, 1, 1, 1, 1, 1, 1, true));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16, {12, 12, 1, 2560}, {3, 3, 1, 2560}, 1, 1, 1, 1, 1, 1, true));
```

```cpp
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16, {12, 12, 2, 1024}, {3, 3, 2, 1024}, 1, 1, 1, 1, 1, 1, true));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16, {12, 12, 1, 2048}, {3, 3, 1, 2048}, 1, 1, 1, 1, 1, 1, true));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16, {12, 12, 2, 2048}, {3, 3, 2, 2048}, 1, 1, 1, 1, 1, 1, true));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16, {12, 12, 1, 2560}, {3, 3, 1, 2560}, 1, 1, 1, 1, 1, 1, true));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16, {12, 12, 2, 2560}, {3, 3, 2, 2560}, 1, 1, 1, 1, 1, 1, true));
```

```cpp
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16, {12, 12, 1, 2048}, {3, 3, 1, 2048}, 1, 1, 1, 1, 1, 1, true));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16, {12, 12, 2, 2048}, {3, 3, 2, 2048}, 1, 1, 1, 1, 1, 1, true));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16, {12, 12, 1, 2560}, {3, 3, 1, 2560}, 1, 1, 1, 1, 1, 1, true));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16, {12, 12, 2, 2560}, {3, 3, 2, 2560}, 1, 1, 1, 1, 1, 1, true));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16, {5, 5, 1, 32}, {3, 4, 1, 32}, 1, 1, 0, 0, 1, 1, true));
```

```cpp
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16, {12, 12, 2, 2048}, {3, 3, 2, 2048}, 1, 1, 1, 1, 1, 1, true));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16, {12, 12, 1, 2560}, {3, 3, 1, 2560}, 1, 1, 1, 1, 1, 1, true));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16, {12, 12, 2, 2560}, {3, 3, 2, 2560}, 1, 1, 1, 1, 1, 1, true));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16, {5, 5, 1, 32}, {3, 4, 1, 32}, 1, 1, 0, 0, 1, 1, true));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F32, GGML_TYPE_F32, {2, 2, 1536, 729}, {2, 2, 1536, 4096}, 1, 1, 0, 0, 1, 1, true));
```

```cpp
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16, {12, 12, 1, 2560}, {3, 3, 1, 2560}, 1, 1, 1, 1, 1, 1, true));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16, {12, 12, 2, 2560}, {3, 3, 2, 2560}, 1, 1, 1, 1, 1, 1, true));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16, {5, 5, 1, 32}, {3, 4, 1, 32}, 1, 1, 0, 0, 1, 1, true));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F32, GGML_TYPE_F32, {2, 2, 1536, 729}, {2, 2, 1536, 4096}, 1, 1, 0, 0, 1, 1, true));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16, {128, 128, 1, 2}, {32, 33, 1, 2}, 1, 1, 1, 1, 1, 1, true));
```

```cpp
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16, {12, 12, 2, 2560}, {3, 3, 2, 2560}, 1, 1, 1, 1, 1, 1, true));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16, {5, 5, 1, 32}, {3, 4, 1, 32}, 1, 1, 0, 0, 1, 1, true));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F32, GGML_TYPE_F32, {2, 2, 1536, 729}, {2, 2, 1536, 4096}, 1, 1, 0, 0, 1, 1, true));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16, {128, 128, 1, 2}, {32, 33, 1, 2}, 1, 1, 1, 1, 1, 1, true));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16, {128, 128, 2, 1}, {33, 34, 2, 1}, 1, 1, 1, 1, 1, 1, true));
```

```cpp
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16, {5, 5, 1, 32}, {3, 4, 1, 32}, 1, 1, 0, 0, 1, 1, true));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F32, GGML_TYPE_F32, {2, 2, 1536, 729}, {2, 2, 1536, 4096}, 1, 1, 0, 0, 1, 1, true));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16, {128, 128, 1, 2}, {32, 33, 1, 2}, 1, 1, 1, 1, 1, 1, true));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16, {128, 128, 2, 1}, {33, 34, 2, 1}, 1, 1, 1, 1, 1, 1, true));

```

```cpp
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F32, GGML_TYPE_F32, {2, 2, 1536, 729}, {2, 2, 1536, 4096}, 1, 1, 0, 0, 1, 1, true));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16, {128, 128, 1, 2}, {32, 33, 1, 2}, 1, 1, 1, 1, 1, 1, true));
    test_cases.emplace_back(new test_im2col(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16, {128, 128, 2, 1}, {33, 34, 2, 1}, 1, 1, 1, 1, 1, 1, true));

    // im2col 3D
```

```cpp

    // im2col 3D
    test_cases.emplace_back(new test_im2col_3d(GGML_TYPE_F32, GGML_TYPE_F32, GGML_TYPE_F32));
    test_cases.emplace_back(new test_im2col_3d(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F32));
    test_cases.emplace_back(new test_im2col_3d(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16));
```

```cpp
    // im2col 3D
    test_cases.emplace_back(new test_im2col_3d(GGML_TYPE_F32, GGML_TYPE_F32, GGML_TYPE_F32));
    test_cases.emplace_back(new test_im2col_3d(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F32));
    test_cases.emplace_back(new test_im2col_3d(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16));
    for (int s0 : {1, 3}) {
```

```cpp
    test_cases.emplace_back(new test_im2col_3d(GGML_TYPE_F32, GGML_TYPE_F32, GGML_TYPE_F32));
    test_cases.emplace_back(new test_im2col_3d(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F32));
    test_cases.emplace_back(new test_im2col_3d(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16));
    for (int s0 : {1, 3}) {
        for (int s1 : {1, 3}) {
```

```cpp
    test_cases.emplace_back(new test_im2col_3d(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F32));
    test_cases.emplace_back(new test_im2col_3d(GGML_TYPE_F32, GGML_TYPE_F16, GGML_TYPE_F16));
    for (int s0 : {1, 3}) {
        for (int s1 : {1, 3}) {
            for (int s2 : {1, 3}) {
```

```cpp
    };

    for (auto kernel_type : {GGML_TYPE_F32, GGML_TYPE_F16}) {
        for (auto act_case : cases) {
            test_cases.emplace_back(new test_conv_2d(
```

```cpp
    // CONV_2D:
    auto calc_conv_output_size = [](int64_t ins, int64_t ks, int s, int p, int d) -> int64_t {
        return (ins + 2 * p - d * (ks - 1) - 1) / s + 1;
    };

```

Verdict (recorded in `defect_review.json`):

- `verdict`: `REVIEWED`
- `is_true_positive`: `false` — **false positive**
- `failure_reason`: `ownership`
- `needs_dfg`: `false`
- `missing_capability`: `` 容器所有权（匿名 `new` 无身份） ``
- `confidence`: `0.9`
- `reason`: `` tests/test-backend-ops.cpp:9177 `emplace_back(new test_im2col(...))`，容器是 8947 声明的 `std::vector<std::unique_ptr<test_case>>`。 ``
- `annotator`: `AI 提议 + 人工确认（Claude Code 提议 / Kyber5323 逐条确认，2026-09-22）`
- `notes`: `null`

### DFV1-redis50-cd2748a1c5b013cd

- type: `4.1` / `resource_lifetime`
- status: `resolved` (confidence 0.6)
- subject: `new_str` in `HashNotifyCallback`
- candidate: `E-DEFECT-da356b3fe4e3fae114278875`
- anchor: `event:eeea192a98f22f270615b66cfb181e5613f98b14127b5feaff1775024623ba94`
- source: `tests/modules/keymeta_notify.c:55-55`, `tests/modules/keymeta_notify.c:56-56`, `tests/modules/keymeta_notify.c:62-62`, `tests/modules/keymeta_notify.c:63-63`

Facts:

- `event`='ALLOC', `matched_name`='strdup', `method`='HashNotifyCallback', `point`='event:eeea192a98f22f270615b66cfb181e5613f98b14127b5feaff1775024623ba94', `source`='tests/modules/keymeta_notify.c:55-55', `subject`='new_str', `subject_from`='assignment'
- `event`='CALL', `matched_name`='call_expression', `method`='HashNotifyCallback', `point`='event:c4c61b37459b3a50c9c26ccbbcb3b46bf1223965fd5976ee6d9a5df3ce634117', `source`='tests/modules/keymeta_notify.c:55-55', `subject`='new_str', `subject_from`='assignment'
- `event`='CALL', `matched_name`='call_expression', `method`='HashNotifyCallback', `point`='event:5b3755d64bae5e3ab6ab625539f2f783a8e920bc9ac0b357ea501a287fbb2b20', `source`='tests/modules/keymeta_notify.c:56-56', `subject`='meta_class_id', `subject_from`='argument'
- `event`='BRANCH', `matched_name`='RedisModule_SetKeyMeta(meta_class_id,k,(uint64_t)new_str)==REDISMODULE_OK', `method`='HashNotifyCallback', `point`='event:411635e51c44ea36ebbd3ac4b9d764d34eb44ec6f131e653fa035c1cca0fac2b', `source`='tests/modules/keymeta_notify.c:56-56', `subject`='', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='HashNotifyCallback', `point`='event:a1feb0efcce7001383482d268df4efdb9935de31ed6a0c053f9402e1dc460a8f', `source`='tests/modules/keymeta_notify.c:62-62', `subject`='k', `subject_from`='argument'
- `event`='RETURN', `matched_name`='return_statement', `method`='HashNotifyCallback', `point`='event:50d8493bb7a036f2247cafcfaaa5aae3d9047c889dfd7a971097aecf8c8fbfdd', `source`='tests/modules/keymeta_notify.c:63-63', `subject`='REDISMODULE_OK', `subject_from`='return'

Uncertain facts:


Missing evidence:

- the ownership contract of a callee: whether a function the resource is passed to takes ownership, borrows it, or frees it
- RAII and smart pointers: `unique_ptr<T> p(new T)` is an ALLOC with no RELEASE in the graph, so every owning smart pointer reads as a leak
- aliasing and reassignment: `q = p; free(q)`, `free(p); p = NULL` and `p = realloc(p, n)` are all invisible without a data-flow graph
- a release performed inside a callee: the graph only sees RELEASE points in the method it walked

```c

    /* Set new metadata - a simple string "notified" */
    char *new_str = strdup("notified");
    if (RedisModule_SetKeyMeta(meta_class_id, k, (uint64_t)new_str) == REDISMODULE_OK) {
        meta_set_count++;
```

```c
    /* Set new metadata - a simple string "notified" */
    char *new_str = strdup("notified");
    if (RedisModule_SetKeyMeta(meta_class_id, k, (uint64_t)new_str) == REDISMODULE_OK) {
        meta_set_count++;
    } else {
```

```c
    }

    RedisModule_CloseKey(k);
    return REDISMODULE_OK;
}
```

```c

    RedisModule_CloseKey(k);
    return REDISMODULE_OK;
}

```

Verdict (recorded in `defect_review.json`):

- `verdict`: `REVIEWED`
- `is_true_positive`: `false` — **false positive**
- `failure_reason`: `call_argument_loss`
- `needs_dfg`: `false`
- `missing_capability`: `第 N 实参交接 + callee 的所有权契约`
- `confidence`: `0.8`
- `reason`: `` `new_str`（tests/modules/keymeta_notify.c:55）作为第 3 实参交给 `RedisModule_SetKeyMeta`（56），成功路径由 `MetaFreeCallback` 释放（68-72），失败路径当场 free（59）。 ``
- `annotator`: `AI 提议 + 人工确认（Claude Code 提议 / Kyber5323 逐条确认，2026-09-22）`
- `notes`: `选 call_argument_loss 而非 contract：病根是 callee 取所有权，但这条 FP 的机制是 CALL 只记第一个主体、第 3 实参的交接根本没被看见。`

### DFV1-llamacpp100-5a2cb3f09b71573a

- type: `4.6` / `resource_lifetime`
- status: `ambiguous` (confidence 0.4)
- subject: `lk` in `server_models.get_all_meta`
- candidate: `E-DEFECT-9f9c865bf9a38738b10284bd`
- anchor: `event:a1b6b1d139f55acc7b632292b3e91e9d6b08edc9536f6e9a594ecafee7ec91a4`
- source: `tools/server/server-models.cpp:1068-1068`, `tools/server/server-models.cpp:1072-1072`, `tools/server/server-models.cpp:1073-1073`, `tools/server/server-models.cpp:1076-1076`

Facts:

- `event`='LOCK', `matched_name`='lock', `method`='server_models.get_all_meta', `point`='event:a1b6b1d139f55acc7b632292b3e91e9d6b08edc9536f6e9a594ecafee7ec91a4', `source`='tools/server/server-models.cpp:1068-1068', `subject`='lk', `subject_from`='receiver'
- `event`='CALL', `matched_name`='call_expression', `method`='server_models.get_all_meta', `point`='event:0faf0618728a8b5ceb85b7318b07b8538e8721e404d6efa2eaf551adc43739d8', `source`='tools/server/server-models.cpp:1072-1072', `subject`='mapping', `subject_from`='receiver'
- `event`='CALL', `matched_name`='call_expression', `method`='server_models.get_all_meta', `point`='event:8ef941f647b332a9db3b325470a4a916e6bcd95b31c367ec0468dfa51d412f64', `source`='tools/server/server-models.cpp:1072-1072', `subject`='', `subject_from`=''
- `event`='BRANCH', `matched_name`='mapping', `method`='server_models.get_all_meta', `point`='event:34b1d3e871d33845624b09adbc67a878cbd19db3591b9f18aa6af90440b03023', `source`='tools/server/server-models.cpp:1073-1073', `subject`='', `subject_from`=''
- `event`='RETURN', `matched_name`='return_statement', `method`='server_models.get_all_meta', `point`='event:96e704043011547bf69751c5d3ae60c0dd0d0c6f2b080f7eba7175dc3f582265', `source`='tools/server/server-models.cpp:1076-1076', `subject`='result', `subject_from`='return'

Uncertain facts:

- `decisive`=True, `detail`='the lock was recovered from a receiver or a declaration, so the name identifies an expression, not the object it names', `fact`='LOCK_IDENTITY', `point`='event:a1b6b1d139f55acc7b632292b3e91e9d6b08edc9536f6e9a594ecafee7ec91a4', `source`='tools/server/server-models.cpp:1068-1068', `status`='ambiguous'

Missing evidence:

- the ownership contract of a callee: whether a function the resource is passed to takes ownership, borrows it, or frees it
- RAII and smart pointers: `unique_ptr<T> p(new T)` is an ALLOC with no RELEASE in the graph, so every owning smart pointer reads as a leak
- aliasing and reassignment: `q = p; free(q)`, `free(p); p = NULL` and `p = realloc(p, n)` are all invisible without a data-flow graph
- a release performed inside a callee: the graph only sees RELEASE points in the method it walked
- whether the error path is reachable in practice: the graph reports the path exists, not that the input reaches it
- whether a destructor or another path releases the lock first
- RAII lock recall: `std::lock_guard<T> lock(m);` parses as a function declaration and is promoted to a symbol of its own, so only the brace spelling `lock{m}` carries a lock identity; a guard acquired this way is invisible to this walk

```cpp
        lk.unlock();
        load_models();
        lk.lock();
    }

```

```cpp

    std::vector<server_model_meta> result;
    result.reserve(mapping.size());
    for (const auto & [name, inst] : mapping) {
        result.push_back(inst.meta);
```

```cpp
    std::vector<server_model_meta> result;
    result.reserve(mapping.size());
    for (const auto & [name, inst] : mapping) {
        result.push_back(inst.meta);
    }
```

```cpp
        result.push_back(inst.meta);
    }
    return result;
}

```

Verdict (recorded in `defect_review.json`):

- `verdict`: `REVIEWED`
- `is_true_positive`: `false` — **false positive**
- `failure_reason`: `ownership`
- `needs_dfg`: `false`
- `missing_capability`: `RAII：unique_lock 析构解锁`
- `confidence`: `0.95`
- `reason`: `` tools/server/server-models.cpp:1064 `std::unique_lock<std::mutex> lk(mutex);`，1066-1068 是 `lk.unlock(); load_models(); lk.lock();`，析构还会兜底。 ``
- `annotator`: `AI 提议 + 人工确认（Claude Code 提议 / Kyber5323 逐条确认，2026-09-22）`
- `notes`: `null`

### DFV1-redis50-d34a8eec3e234975

- type: `4.6` / `resource_lifetime`
- status: `ambiguous` (confidence 0.4)
- subject: `index` in `hnsw_acquire_read_slot`
- candidate: `E-DEFECT-82c1ce11436bbc3b62fa914c`
- anchor: `event:cd0714a64a8a24e02e41e874677e25c76dd62e2ef3f798a360d6a9ae3afb137d`
- source: `modules/vector-sets/hnsw.c:2029-2029`, `modules/vector-sets/hnsw.c:2032-2032`, `modules/vector-sets/hnsw.c:2037-2037`

Facts:

- `event`='LOCK', `matched_name`='pthread_mutex_lock', `method`='hnsw_acquire_read_slot', `point`='event:cd0714a64a8a24e02e41e874677e25c76dd62e2ef3f798a360d6a9ae3afb137d', `source`='modules/vector-sets/hnsw.c:2029-2029', `subject`='index', `subject_from`='argument'
- `event`='BRANCH', `matched_name`='pthread_mutex_lock(&index->slot_locks[slot])!=0', `method`='hnsw_acquire_read_slot', `point`='event:b68a909d0a4e3a3b4c66a7ab012e6dedf6beb5f047a833697a74efcd91e9e83c', `source`='modules/vector-sets/hnsw.c:2029-2029', `subject`='', `subject_from`=''
- `event`='RETURN', `matched_name`='return_statement', `method`='hnsw_acquire_read_slot', `point`='event:75d6ac51a2f034527b9fc85e5e6447f5fd50eb1081daedab8c8fd1547855472d', `source`='modules/vector-sets/hnsw.c:2029-2029', `subject`='', `subject_from`=''
- `event`='DEREFERENCE', `matched_name`='index->global_lock', `method`='hnsw_acquire_read_slot', `point`='event:bae2fa9128aa1ae307190f868b6c5b2c2164f0ff7bf77b2ff8f983eb037c42e3', `source`='modules/vector-sets/hnsw.c:2032-2032', `subject`='index', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='hnsw_acquire_read_slot', `point`='event:d9fcb81c42d6d419eee50d92f7b649e7008ef547f09b18cc94ff66d406f5b299', `source`='modules/vector-sets/hnsw.c:2032-2032', `subject`='index', `subject_from`='argument'
- `event`='LOCK', `matched_name`='pthread_rwlock_rdlock', `method`='hnsw_acquire_read_slot', `point`='event:b16c7356168c168c4139c816ed13c0875f5d73cfe433921cf0e132ee0beeb43a', `source`='modules/vector-sets/hnsw.c:2032-2032', `subject`='index', `subject_from`='argument'
- `event`='BRANCH', `matched_name`='pthread_rwlock_rdlock(&index->global_lock)!=0', `method`='hnsw_acquire_read_slot', `point`='event:a145da0b30a32b5e43a7ee51c7a21610331ac7cd626ae13520d0caf72fd491e7', `source`='modules/vector-sets/hnsw.c:2032-2032', `subject`='', `subject_from`=''
- `event`='RETURN', `matched_name`='return_statement', `method`='hnsw_acquire_read_slot', `point`='event:37ecd57ec1cef32e74ce39225aae9f370bf3328105fad2efef93fe9450ef5d66', `source`='modules/vector-sets/hnsw.c:2037-2037', `subject`='slot', `subject_from`='return'

Uncertain facts:

- `decisive`=True, `detail`='the name is the root of a longer expression (`s` in `s->buf`), so the same name elsewhere may be a different object', `fact`='SUBJECT_PATH', `point`='event:cd0714a64a8a24e02e41e874677e25c76dd62e2ef3f798a360d6a9ae3afb137d', `source`='modules/vector-sets/hnsw.c:2029-2029', `status`='ambiguous'

Missing evidence:

- the ownership contract of a callee: whether a function the resource is passed to takes ownership, borrows it, or frees it
- RAII and smart pointers: `unique_ptr<T> p(new T)` is an ALLOC with no RELEASE in the graph, so every owning smart pointer reads as a leak
- aliasing and reassignment: `q = p; free(q)`, `free(p); p = NULL` and `p = realloc(p, n)` are all invisible without a data-flow graph
- a release performed inside a callee: the graph only sees RELEASE points in the method it walked
- whether the error path is reachable in practice: the graph reports the path exists, not that the input reaches it
- whether a destructor or another path releases the lock first
- RAII lock recall: `std::lock_guard<T> lock(m);` parses as a function declaration and is promoted to a symbol of its own, so only the brace spelling `lock{m}` carries a lock identity; a guard acquired this way is invisible to this walk

```c

    /* Try to lock the selected slot. */
    if (pthread_mutex_lock(&index->slot_locks[slot]) != 0) return -1;

    /* Get read lock. */
```

```c

    /* Get read lock. */
    if (pthread_rwlock_rdlock(&index->global_lock) != 0) {
        pthread_mutex_unlock(&index->slot_locks[slot]);
        return -1;
```

```c
    }

    return slot;
}

```

Verdict (recorded in `defect_review.json`):

- `verdict`: `REVIEWED`
- `is_true_positive`: `false` — **false positive**
- `failure_reason`: `contract`
- `needs_dfg`: `false`
- `missing_capability`: `acquire/release 跨帧契约（成对的 API，不在同一个帧里）`
- `confidence`: `0.9`
- `reason`: `` modules/vector-sets/hnsw.c:2029 的加锁由调用方经 `hnsw_release_read_slot`（2043-2046）释放——acquire/release 成对的 API 契约。 ``
- `annotator`: `AI 提议 + 人工确认（Claude Code 提议 / Kyber5323 逐条确认，2026-09-22）`
- `notes`: `null`

### DFV1-llamacpp100-72bae08d3ad34028

- type: `4.6` / `resource_lifetime`
- status: `resolved` (confidence 0.6)
- subject: `pl` in `vk_device_struct.~vk_device_struct`
- candidate: `E-DEFECT-0a7c649ec8f965f0841755fb`
- anchor: `event:d1df2a906d937337c9e88f08ac808ac43879203b06efae639d8bb625364f4c6a`
- source: `ggml/src/ggml-vulkan/ggml-vulkan.cpp:15928-15928`, `ggml/src/ggml-vulkan/ggml-vulkan.cpp:15929-15929`, `ggml/src/ggml-vulkan/ggml-vulkan.cpp:15923-15923`, `ggml/src/ggml-vulkan/ggml-vulkan.cpp:15931-15931`, `ggml/src/ggml-vulkan/ggml-vulkan.cpp:15933-15933`, `ggml/src/ggml-vulkan/ggml-vulkan.cpp:15935-15935`

Facts:

- `event`='LOCK', `matched_name`='lock', `method`='vk_device_struct.~vk_device_struct', `point`='event:d1df2a906d937337c9e88f08ac808ac43879203b06efae639d8bb625364f4c6a', `source`='ggml/src/ggml-vulkan/ggml-vulkan.cpp:15928-15928', `subject`='pl', `subject_from`='assignment'
- `event`='CALL', `matched_name`='call_expression', `method`='vk_device_struct.~vk_device_struct', `point`='event:1bb698e797f869c88959fe613074e6a01b6ac7b370f33bea9862324b2ae74080', `source`='ggml/src/ggml-vulkan/ggml-vulkan.cpp:15929-15929', `subject`='device', `subject_from`='argument'
- `event`='BRANCH', `matched_name`='all_pipelines', `method`='vk_device_struct.~vk_device_struct', `point`='event:7e67eba7652407ff9f8fb020c752dc168c4ec42a32d027e7c0204d4b3f5d786e', `source`='ggml/src/ggml-vulkan/ggml-vulkan.cpp:15923-15923', `subject`='', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='vk_device_struct.~vk_device_struct', `point`='event:176610fe8985c7ea378c1750f99d5e09fe82519458cbbc899c5c3f1c89b248f8', `source`='ggml/src/ggml-vulkan/ggml-vulkan.cpp:15931-15931', `subject`='all_pipelines', `subject_from`='receiver'
- `event`='CALL', `matched_name`='call_expression', `method`='vk_device_struct.~vk_device_struct', `point`='event:cfe3b3d9fdb34a8bf0d9e19612c16fc15b7f6bfa09513aacf295e3b5d3f57f95', `source`='ggml/src/ggml-vulkan/ggml-vulkan.cpp:15933-15933', `subject`='dsl', `subject_from`='argument'
- `event`='CALL', `matched_name`='call_expression', `method`='vk_device_struct.~vk_device_struct', `point`='event:6fc3f65b83c5b47eb00672b52809c7d32c5e6256e2c80a8ef985353f6c2bd844', `source`='ggml/src/ggml-vulkan/ggml-vulkan.cpp:15935-15935', `subject`='device', `subject_from`='receiver'

Uncertain facts:


Missing evidence:

- the ownership contract of a callee: whether a function the resource is passed to takes ownership, borrows it, or frees it
- RAII and smart pointers: `unique_ptr<T> p(new T)` is an ALLOC with no RELEASE in the graph, so every owning smart pointer reads as a leak
- aliasing and reassignment: `q = p; free(q)`, `free(p); p = NULL` and `p = realloc(p, n)` are all invisible without a data-flow graph
- a release performed inside a callee: the graph only sees RELEASE points in the method it walked
- whether the error path is reachable in practice: the graph reports the path exists, not that the input reaches it
- whether a destructor or another path releases the lock first
- RAII lock recall: `std::lock_guard<T> lock(m);` parses as a function declaration and is promoted to a symbol of its own, so only the brace spelling `lock{m}` carries a lock identity; a guard acquired this way is invisible to this walk

```cpp
        }

        vk_pipeline pl = pipeline.lock();
        ggml_vk_destroy_pipeline(device, pl);
    }
```

```cpp

        vk_pipeline pl = pipeline.lock();
        ggml_vk_destroy_pipeline(device, pl);
    }
    all_pipelines.clear();
```

```cpp
    transfer_queue.reset();

    for (auto& pipeline : all_pipelines) {
        if (pipeline.expired()) {
            continue;
```

```cpp
        ggml_vk_destroy_pipeline(device, pl);
    }
    all_pipelines.clear();

    device.destroyDescriptorSetLayout(dsl);
```

```cpp
    all_pipelines.clear();

    device.destroyDescriptorSetLayout(dsl);

    device.destroy();
```

```cpp
    device.destroyDescriptorSetLayout(dsl);

    device.destroy();
}

```

Verdict (recorded in `defect_review.json`):

- `verdict`: `REVIEWED`
- `is_true_positive`: `false` — **false positive**
- `failure_reason`: `extractor`
- `needs_dfg`: `false`
- `missing_capability`: `` 事件提取正确性：receiver 类型（`weak_ptr::lock` 不是互斥量） ``
- `confidence`: `0.9`
- `reason`: `` ggml/src/ggml-vulkan/ggml-vulkan.cpp:15928 `vk_pipeline pl = pipeline.lock();`，`pipeline` 是 `std::weak_ptr`，`.lock()` 是提升弱引用，LOCK 事件本身是错的。 ``
- `annotator`: `AI 提议 + 人工确认（Claude Code 提议 / Kyber5323 逐条确认，2026-09-22）`
- `notes`: `也可辩称归 identity；此处名字就是那个对象，错的是方法语义，故归 extractor。`

### DFV1-redis50-4009b8116c4cf958

- type: `4.6` / `resource_lifetime`
- status: `resolved` (confidence 0.6)
- subject: `moduleGIL` in `moduleAcquireGIL`
- candidate: `E-DEFECT-8986a6aa5b25f9bcef433d8f`
- anchor: `event:57cd51afa27631d38a50741575083631b250da3b0dbba3b43ae7e10ba7c62fdf`
- source: `src/module.c:9272-9272`

Facts:

- `event`='LOCK', `matched_name`='pthread_mutex_lock', `method`='moduleAcquireGIL', `point`='event:57cd51afa27631d38a50741575083631b250da3b0dbba3b43ae7e10ba7c62fdf', `source`='src/module.c:9272-9272', `subject`='moduleGIL', `subject_from`='argument'

Uncertain facts:


Missing evidence:

- the ownership contract of a callee: whether a function the resource is passed to takes ownership, borrows it, or frees it
- RAII and smart pointers: `unique_ptr<T> p(new T)` is an ALLOC with no RELEASE in the graph, so every owning smart pointer reads as a leak
- aliasing and reassignment: `q = p; free(q)`, `free(p); p = NULL` and `p = realloc(p, n)` are all invisible without a data-flow graph
- a release performed inside a callee: the graph only sees RELEASE points in the method it walked
- whether the error path is reachable in practice: the graph reports the path exists, not that the input reaches it
- whether a destructor or another path releases the lock first
- RAII lock recall: `std::lock_guard<T> lock(m);` parses as a function declaration and is promoted to a symbol of its own, so only the brace spelling `lock{m}` carries a lock identity; a guard acquired this way is invisible to this walk

```c

void moduleAcquireGIL(void) {
    pthread_mutex_lock(&moduleGIL);
    threadHoldsGIL = 1;
}
```

Verdict (recorded in `defect_review.json`):

- `verdict`: `REVIEWED`
- `is_true_positive`: `false` — **false positive**
- `failure_reason`: `contract`
- `needs_dfg`: `false`
- `missing_capability`: `acquire/release 跨帧契约（成对的 API，不在同一个帧里）`
- `confidence`: `0.95`
- `reason`: `` `moduleAcquireGIL()`（src/module.c:9271-9273）加锁，配对的 `moduleReleaseGIL()`（9282-9285）解锁；调用点成对（2543/2545、9260/9223）。 ``
- `annotator`: `AI 提议 + 人工确认（Claude Code 提议 / Kyber5323 逐条确认，2026-09-22）`
- `notes`: `null`

### DFV1-llamacpp100-de09d340590421f1

- type: `1.1` / `race_condition`
- status: `ambiguous` (confidence 0.5)
- subject: `v` in `ggml_validate_row_data`
- candidate: `E-DEFECT-29af50bdd924320c99f2dbba`
- anchor: `event:2997ade83dce04efe13b97dd39714f1cc89d43daf873078f18b4d03e32cd7dc9`
- source: `ggml/src/ggml-quants.c:5447-5447`, `ggml/src/ggml-quants.c:5488-5488`, `ggml/src/ggml-quants.c:5462-5462`, `ggml/src/ggml-quants.c:5503-5503`, `ggml/src/ggml-openvino/utils.cpp:1707-1707`

Facts:

- `event`='CALL', `matched_name`='call_expression', `method`='ggml_validate_row_data', `point`='event:2997ade83dce04efe13b97dd39714f1cc89d43daf873078f18b4d03e32cd7dc9', `source`='ggml/src/ggml-quants.c:5447-5447', `subject`='v', `subject_from`='assignment'
- `event`='CALL', `matched_name`='call_expression', `method`='ggml_validate_row_data', `point`='event:388c69348406599b3dbcccf4fad5b08f59dea485b496790e0129a30df44cf1af', `source`='ggml/src/ggml-quants.c:5488-5488', `subject`='v', `subject_from`='assignment'
- `event`='CALL', `matched_name`='call_expression', `method`='ggml_validate_row_data', `point`='event:3b4b6e77a0f81fa2b6d3797564b5c56473e88cac43cde47b37d6511f3b7e4531', `source`='ggml/src/ggml-quants.c:5462-5462', `subject`='v', `subject_from`='assignment'
- `event`='CALL', `matched_name`='call_expression', `method`='ggml_validate_row_data', `point`='event:9fea9fdfa1678ddcb3a1016b430597200518a83f399c58117445f4fd7b1fe1d4', `source`='ggml/src/ggml-quants.c:5503-5503', `subject`='v', `subject_from`='assignment'
- `event`='CALL', `matched_name`='call_expression', `method`='print_output_tensor_info', `point`='event:e29806dd7185722cca9a3f7f98ddad31b85022d93158a3e6f1c9995c49a03520', `source`='ggml/src/ggml-openvino/utils.cpp:1707-1707', `subject`='v', `subject_from`='assignment'

Uncertain facts:

- `decisive`=True, `detail`='the repository starts threads somewhere, so neither method is ordered', `fact`='MAY_PARALLEL', `status`='ambiguous'
- `decisive`=True, `detail`='no lock in this method', `fact`='PROTECTED_BY', `lock`='', `method`='ggml_validate_row_data', `status`='ambiguous'
- `decisive`=True, `detail`='no lock in this method', `fact`='PROTECTED_BY', `lock`='', `method`='print_output_tensor_info', `status`='ambiguous'
- `decisive`=True, `detail`='write/read decided syntactically: write vs write', `fact`='ACCESS_KIND', `status`='ambiguous'

Missing evidence:

- READ/WRITE distinction (level 1, DFG): access kind is a syntactic proxy here, so write-write and read-write cannot be separated from read-read, and a shared variable that never appears in an event-bearing expression (`counter++`) produces no event at all
- whether the two contexts really run concurrently: MAY_PARALLEL is undecidable from spawn points alone
- whether the lock actually guards this variable: holding a lock near an access is not the same as the lock protecting the data
- whether the variable tolerates weak consistency (a statistics counter may be allowed to race by design)

```c
#if defined(__AVX2__)
                for (; i + 15 < nb; i += 16) {
                    __m256i v = _mm256_loadu_si256((const __m256i *)(f + i));
                    __m256i vexp = _mm256_and_si256(v, _mm256_set1_epi16(0x7c00));
                    __m256i cmp = _mm256_cmpeq_epi16(vexp, _mm256_set1_epi16(0x7c00));
```

```c
#if defined(__AVX2__)
                for (; i + 7 < nb; i += 8) {
                    __m256i v = _mm256_loadu_si256((const __m256i *)(f + i));
                    __m256i vexp = _mm256_and_si256(v, _mm256_set1_epi32(0x7f800000));
                    __m256i cmp = _mm256_cmpeq_epi32(vexp, _mm256_set1_epi32(0x7f800000));
```

```c
#elif defined(__ARM_NEON)
                for (; i + 7 < nb; i += 8) {
                    uint16x8_t v = vld1q_u16(f + i);
                    uint16x8_t vexp = vandq_u16(v, vdupq_n_u16(0x7c00));
                    uint16x8_t cmp = vceqq_u16(vexp, vdupq_n_u16(0x7c00));
```

```c
#elif defined(__ARM_NEON)
                for (; i + 3 < nb; i += 4) {
                    uint32x4_t v = vld1q_u32((const uint32_t *)f + i);
                    uint32x4_t vexp = vandq_u32(v, vdupq_n_u32(0x7f800000));
                    uint32x4_t cmp = vceqq_u32(vexp, vdupq_n_u32(0x7f800000));
```

```cpp

        for (size_t i = 1; i < size; ++i) {
            float v = get_value(i);
            min = std::min(v, min);
            max = std::max(v, max);
```

Verdict (recorded in `defect_review.json`):

- `verdict`: `REVIEWED`
- `is_true_positive`: `false` — **false positive**
- `failure_reason`: `identity`
- `needs_dfg`: `false`
- `missing_capability`: `声明身份与作用域（declaration identity / scope）`
- `confidence`: `0.95`
- `reason`: `` `__m256i v`（ggml/src/ggml-quants.c:5447）是循环内局部 SIMD 变量，另一侧 `float v = get_value(i);`（ggml/src/ggml-openvino/utils.cpp:1707）也是局部量。 ``
- `annotator`: `AI 提议 + 人工确认（Claude Code 提议 / Kyber5323 逐条确认，2026-09-22）`
- `notes`: `null`

### DFV1-redis50-d5f9ea64ceca6552

- type: `1.1` / `race_condition`
- status: `ambiguous` (confidence 0.5)
- subject: `s` in `streamTrim`
- candidate: `E-DEFECT-df3e20ce3e2f699a040099fd`
- anchor: `event:065b2246858af2b271947a4f6fc3ccaf6b0f60fc9ff8cf7176a669f44c7459eb`
- source: `src/t_stream.c:904-904`, `src/t_stream.c:1046-1046`, `src/t_stream.c:902-902`, `src/t_stream.c:1004-1004`, `src/t_stream.c:1042-1042`, `src/t_stream.c:1017-1017`, `src/t_stream.c:3452-3452`, `src/t_stream.c:868-868`, `src/t_stream.c:1043-1043`, `src/t_stream.c:992-992`, `src/t_stream.c:906-906`, `src/t_stream.c:1002-1002`, `src/t_stream.c:1016-1016`, `src/t_stream.c:983-983`, `src/t_stream.c:955-955`, `src/t_stream.c:863-863`, `src/t_stream.c:885-885`, `src/t_stream.c:1044-1044`

Facts:

- `event`='DEREFERENCE', `matched_name`='s->rax', `method`='streamTrim', `point`='event:065b2246858af2b271947a4f6fc3ccaf6b0f60fc9ff8cf7176a669f44c7459eb', `source`='src/t_stream.c:904-904', `subject`='s', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='streamTrim', `point`='event:16e3b49b6e3cb5cf8e87ef9f81613fab7ad4585c44c964f34fc9038a25ea27c8', `source`='src/t_stream.c:1046-1046', `subject`='s', `subject_from`='argument'
- `event`='CALL', `matched_name`='call_expression', `method`='streamTrim', `point`='event:1eb8d5da62cbdce190bd22aca5b6e48fdaa6bbbb5f0620c420058222fd8dc4ff', `source`='src/t_stream.c:902-902', `subject`='s', `subject_from`='assignment'
- `event`='DEREFERENCE', `matched_name`='s->rax', `method`='streamTrim', `point`='event:32dd8bfe06f55d47e46b98fc2bbccd903c4cbadabad73b47118dc556702f4fc3', `source`='src/t_stream.c:1004-1004', `subject`='s', `subject_from`=''
- `event`='DEREFERENCE', `matched_name`='s->length', `method`='streamTrim', `point`='event:389fb5c3869c045d86cdfe133b4bae6e47bb10c8b6a70cc48f0284c10b70537b', `source`='src/t_stream.c:1042-1042', `subject`='s', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='streamTrim', `point`='event:3d4169ddb473ca75593873137470129f279c13ff88fb6026144c20183e3215f7', `source`='src/t_stream.c:1004-1004', `subject`='s', `subject_from`='argument'
- `event`='CALL', `matched_name`='call_expression', `method`='streamTrim', `point`='event:522b20cad35cbdd6380a5d8a4d9a243a58450c6d6360ea1b5768281806ef98c1', `source`='src/t_stream.c:1017-1017', `subject`='s', `subject_from`='assignment'
- `event`='DEREFERENCE', `matched_name`='s->alloc_size', `method`='streamFreeCG', `point`='event:65071a6b9f7368bf877e0afa22d78feae35e64c46af1a9c3adc8d6f1d95f385f', `source`='src/t_stream.c:3452-3452', `subject`='s', `subject_from`=''
- `event`='DEREFERENCE', `matched_name`='s->length', `method`='streamTrim', `point`='event:714539dcaa86856f6807c57eca92ef5cabe003f7497b6e0d8551c37a9dfd7ae0', `source`='src/t_stream.c:868-868', `subject`='s', `subject_from`=''
- `event`='DEREFERENCE', `matched_name`='s->first_id', `method`='streamTrim', `point`='event:791a7df6e661c3a3637d263e44fd6d5b366b2017234221d8c719c0d1d24cc787', `source`='src/t_stream.c:1043-1043', `subject`='s', `subject_from`=''
- `event`='DEREFERENCE', `matched_name`='s->alloc_size', `method`='streamTrim', `point`='event:7fa877e7e7e22f9e6cc23ed5cf5858c6917a0d8a847948e064007778910dc688', `source`='src/t_stream.c:902-902', `subject`='s', `subject_from`=''
- `event`='DEREFERENCE', `matched_name`='s->first_id', `method`='streamTrim', `point`='event:834d703b43cce3121193cb684f6682e82cd28e1457b2aa232a8a68bb706b8e26', `source`='src/t_stream.c:1046-1046', `subject`='s', `subject_from`=''
- `event`='DEREFERENCE', `matched_name`='s->length', `method`='streamTrim', `point`='event:862b9acb917c59244ea81579c2b6953f18dd8277ae42d56a2a4cce632e4acf6d', `source`='src/t_stream.c:992-992', `subject`='s', `subject_from`=''
- `event`='DEREFERENCE', `matched_name`='s->alloc_size', `method`='streamTrim', `point`='event:93079df133cd96d86909efbfe147622e20f696935add6622295e0b7ad517e4e0', `source`='src/t_stream.c:1017-1017', `subject`='s', `subject_from`=''
- `event`='DEREFERENCE', `matched_name`='s->length', `method`='streamTrim', `point`='event:957245782e925fe60a917e9a3e88e1148243a9a9b81e45c9f6f6c2565ab3d6a1', `source`='src/t_stream.c:906-906', `subject`='s', `subject_from`=''
- `event`='DEREFERENCE', `matched_name`='s->alloc_size', `method`='streamTrim', `point`='event:a7e6a696b175336cefbb6cb0b8a46d3b596fda41be042d2fd9b6364593ebd6ba', `source`='src/t_stream.c:1002-1002', `subject`='s', `subject_from`=''
- `event`='DEREFERENCE', `matched_name`='s->alloc_size', `method`='streamTrim', `point`='event:afaa548f2b5c175f09752853264d53711d230b7ff84ec2456ccc407295c8d156', `source`='src/t_stream.c:1016-1016', `subject`='s', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='streamTrim', `point`='event:b20e97a74c1961e765999c77a5eeecc300ef5a7d5e66fce227c37c74d825965b', `source`='src/t_stream.c:983-983', `subject`='s', `subject_from`='argument'
- `event`='DEREFERENCE', `matched_name`='s->length', `method`='streamTrim', `point`='event:ca1c3e76e634ede9d13a639a2ac4be3c80d90e44518f4c9e25f49ced69450c39', `source`='src/t_stream.c:955-955', `subject`='s', `subject_from`=''
- `event`='DEREFERENCE', `matched_name`='s->rax', `method`='streamTrim', `point`='event:d4f57cb3b8378460e4058e20df8c6eb40b4bfb33950f4b42d3a360343235c05a', `source`='src/t_stream.c:863-863', `subject`='s', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='streamTrim', `point`='event:da526e71722b3d27f1dce4f250916178b100566906f9ddecef606b8a66ade5ed', `source`='src/t_stream.c:904-904', `subject`='s', `subject_from`='argument'
- `event`='DEREFERENCE', `matched_name`='s->length', `method`='streamTrim', `point`='event:df63f1083948d07ad660a0fa6538b0346fd34f979bee19699eb304406576efdc', `source`='src/t_stream.c:885-885', `subject`='s', `subject_from`=''
- `event`='DEREFERENCE', `matched_name`='s->first_id', `method`='streamTrim', `point`='event:f8b0d922ae77df0c6c3c9ed6d6fc794ae774efadf47fcfc797d40cf09f3a3c50', `source`='src/t_stream.c:1044-1044', `subject`='s', `subject_from`=''

Uncertain facts:

- `decisive`=True, `detail`='the repository starts threads somewhere, so neither method is ordered', `fact`='MAY_PARALLEL', `status`='ambiguous'
- `decisive`=True, `detail`='no lock in this method', `fact`='PROTECTED_BY', `lock`='', `method`='streamTrim', `status`='ambiguous'
- `decisive`=True, `detail`='no lock in this method', `fact`='PROTECTED_BY', `lock`='', `method`='streamFreeCG', `status`='ambiguous'
- `decisive`=True, `detail`='write/read decided syntactically: write vs read', `fact`='ACCESS_KIND', `status`='ambiguous'

Missing evidence:

- READ/WRITE distinction (level 1, DFG): access kind is a syntactic proxy here, so write-write and read-write cannot be separated from read-read, and a shared variable that never appears in an event-bearing expression (`counter++`) produces no event at all
- whether the two contexts really run concurrently: MAY_PARALLEL is undecidable from spawn points alone
- whether the lock actually guards this variable: holding a lock near an access is not the same as the lock protecting the data
- whether the variable tolerates weak consistency (a statistics counter may be allowed to race by design)

```c
            s->alloc_size -= lpBytes(lp);
            lpFree(lp);
            raxRemove(s->rax,ri.key,ri.key_len,NULL);
            raxSeek(&ri,">=",ri.key,ri.key_len);
            s->length -= entries;
```

```c
        s->first_id.seq = 0;
    } else if (deleted) {
        streamGetEdgeID(s,1,1,&s->first_id);
    }

```

```c

        if (remove_node) {
            s->alloc_size -= lpBytes(lp);
            lpFree(lp);
            raxRemove(s->rax,ri.key,ri.key_len,NULL);
```

```c
            s->alloc_size -= oldsize;
            lpFree(lp);
            raxRemove(s->rax,ri.key,ri.key_len,NULL);
            raxSeek(&ri,">=",ri.key,ri.key_len);
            continue;
```

```c

    /* Update the stream's first ID after the trimming. */
    if (s->length == 0) {
        s->first_id.ms = 0;
        s->first_id.seq = 0;
```

```c
        p = lpNext(lp,p); /* Skip num-of-fields in the master entry. */
        s->alloc_size -= oldsize;
        s->alloc_size += lpBytes(lp);

        /* Here we should perform garbage collection in case at this point
```

```c
    size_t usable;
    zfree_usable(cg, &usable);
    s->alloc_size -= usable;
}

```

```c
    int64_t deleted = 0;
    while (raxNext(&ri)) {
        if (trim_strategy == TRIM_STRATEGY_MAXLEN && s->length <= maxlen)
            break;

```

```c
    /* Update the stream's first ID after the trimming. */
    if (s->length == 0) {
        s->first_id.ms = 0;
        s->first_id.seq = 0;
    } else if (deleted) {
```

```c
                    lp = lpReplaceInteger(lp, &pcopy, flags);
                    deleted_from_lp++;
                    s->length--;
                    if (p) p = lp + delta;
                }
```

```c
            raxRemove(s->rax,ri.key,ri.key_len,NULL);
            raxSeek(&ri,">=",ri.key,ri.key_len);
            s->length -= entries;
            deleted += entries;
            continue;
```

```c
         * in the node, we can finally remove the entire node. */
        if (node_eligible_for_remove && deleted_from_lp == entries) {
            s->alloc_size -= oldsize;
            lpFree(lp);
            raxRemove(s->rax,ri.key,ri.key_len,NULL);
```

```c
        lp = lpReplaceInteger(lp,&p,marked_deleted+deleted_from_lp);
        p = lpNext(lp,p); /* Skip num-of-fields in the master entry. */
        s->alloc_size -= oldsize;
        s->alloc_size += lpBytes(lp);

```

```c
                } else if (delete_strategy == DELETE_STRATEGY_DELREF) {
                    /* Remove all consumer group references for this entry */
                    streamCleanupEntryCGroupRefs(s, &currid);
                }

```

```c
            int stop;
            if (trim_strategy == TRIM_STRATEGY_MAXLEN) {
                stop = s->length <= maxlen;
            } else {
                /* Following IDs will definitely be greater because the rax
```

```c

    raxIterator ri;
    raxStart(&ri,s->rax);
    raxSeek(&ri,"^",NULL,0);

```

```c
        streamDecodeID(ri.key, &master_id);
        if (trim_strategy == TRIM_STRATEGY_MAXLEN) {
            node_eligible_for_remove = s->length - entries >= maxlen;
        } else {
            /* Read last ID. */
```

```c
    if (s->length == 0) {
        s->first_id.ms = 0;
        s->first_id.seq = 0;
    } else if (deleted) {
        streamGetEdgeID(s,1,1,&s->first_id);
```

Verdict (recorded in `defect_review.json`):

- `verdict`: `REVIEWED`
- `is_true_positive`: `false` — **false positive**
- `failure_reason`: `identity`
- `needs_dfg`: `false`
- `missing_capability`: `声明身份与作用域（declaration identity / scope）`
- `confidence`: `0.95`
- `reason`: `` `s` 是 src/t_stream.c:851 `streamTrim(stream *s, ...)` 的形参，另一侧是 src/t_stream.c:3441 `streamFreeCG(stream *s, streamCG *cg)` 的形参——同名不同对象。 ``
- `annotator`: `AI 提议 + 人工确认（Claude Code 提议 / Kyber5323 逐条确认，2026-09-22）`
- `notes`: `` 这条说明 1.1 的假阳性不是 MAY_PARALLEL 不可解造成的：就算解出并发关系，两个 `s` 也不是同一个对象。 ``

### DFV1-llamacpp100-90604ffec1699c28

- type: `4.1` / `resource_lifetime`
- status: `ambiguous` (confidence 0.4)
- subject: `host_buffer_type` in `ggml_backend_hexagon_device_context.ggml_backend_hexagon_device_context`
- candidate: `E-DEFECT-2df1c48e94864edf440e7dcd`
- anchor: `event:852df4434e2675e61eef57df60cdb7f5e5a0f56768bacf38de6db07450441a10`
- source: `ggml/src/ggml-hexagon/ggml-hexagon.cpp:2109-2109`, `ggml/src/ggml-hexagon/ggml-hexagon.cpp:2113-2113`

Facts:

- `event`='ALLOC', `matched_name`='new_expression', `method`='ggml_backend_hexagon_device_context.ggml_backend_hexagon_device_context', `point`='event:852df4434e2675e61eef57df60cdb7f5e5a0f56768bacf38de6db07450441a10', `source`='ggml/src/ggml-hexagon/ggml-hexagon.cpp:2109-2109', `subject`='host_buffer_type', `subject_from`='assignment'
- `event`='ALLOC', `matched_name`='new_expression', `method`='ggml_backend_hexagon_device_context.ggml_backend_hexagon_device_context', `point`='event:dcda504fb03483efaadd184caa522d31f94feed3efbcc87a22de62acfadfdc3a', `source`='ggml/src/ggml-hexagon/ggml-hexagon.cpp:2113-2113', `subject`='fence_buffer_type', `subject_from`='assignment'

Uncertain facts:

- `decisive`=True, `detail`='the name is the root of a longer expression (`s` in `s->buf`), so the same name elsewhere may be a different object', `fact`='SUBJECT_PATH', `point`='event:852df4434e2675e61eef57df60cdb7f5e5a0f56768bacf38de6db07450441a10', `source`='ggml/src/ggml-hexagon/ggml-hexagon.cpp:2109-2109', `status`='ambiguous'

Missing evidence:

- the ownership contract of a callee: whether a function the resource is passed to takes ownership, borrows it, or frees it
- RAII and smart pointers: `unique_ptr<T> p(new T)` is an ALLOC with no RELEASE in the graph, so every owning smart pointer reads as a leak
- aliasing and reassignment: `q = p; free(q)`, `free(p); p = NULL` and `p = realloc(p, n)` are all invisible without a data-flow graph
- a release performed inside a callee: the graph only sees RELEASE points in the method it walked

```cpp
    host_buffer_type.device  = dev;
    host_buffer_type.iface   = ggml_backend_hexagon_host_buffer_type_interface;
    host_buffer_type.context = new ggml_backend_hexagon_buffer_type_context(config.name + "-HOST", this);

    fence_buffer_type.device  = dev;
```

```cpp
    fence_buffer_type.device  = dev;
    fence_buffer_type.iface   = ggml_backend_hexagon_buffer_type_interface;
    fence_buffer_type.context = new ggml_backend_hexagon_buffer_type_context(config.name + "-FENCE", this);
}

```

Verdict (recorded in `defect_review.json`):

- `verdict`: `REVIEWED`
- `is_true_positive`: `false` — **false positive**
- `failure_reason`: `ownership`
- `needs_dfg`: `false`
- `missing_capability`: `所有权：由析构函数释放`
- `confidence`: `0.95`
- `reason`: `` ggml/src/ggml-hexagon/ggml-hexagon.cpp:2109 在构造函数里分配，`~ggml_backend_hexagon_device_context()`（2117-2119）三处 delete 释放。 ``
- `annotator`: `AI 提议 + 人工确认（Claude Code 提议 / Kyber5323 逐条确认，2026-09-22）`
- `notes`: `null`

### DFV1-redis50-38c4035e8ac62bfc

- type: `1.4` / `lock_order`
- status: `ambiguous` (confidence 0.3)
- subject: `index` in `hnsw_acquire_read_slot`
- candidate: `E-DEFECT-fdb222f2dc473290e6cedc22`
- anchor: `event:cd0714a64a8a24e02e41e874677e25c76dd62e2ef3f798a360d6a9ae3afb137d`
- source: `modules/vector-sets/hnsw.c:2029-2029`, `modules/vector-sets/hnsw.c:2032-2032`

Facts:

- `holding_confirmed`=True, `lock`='index', `method`='hnsw_acquire_read_slot', `method_symbol_id`='symbol:406df0db7bc3af3e4c958ab16ab6d05c3cbf34ba272f0b42ef1f918064f074c6', `point`='event:cd0714a64a8a24e02e41e874677e25c76dd62e2ef3f798a360d6a9ae3afb137d', `role`='first', `side`='single', `source`='modules/vector-sets/hnsw.c:2029-2029'
- `holding_confirmed`=True, `lock`='index', `method`='hnsw_acquire_read_slot', `method_symbol_id`='symbol:406df0db7bc3af3e4c958ab16ab6d05c3cbf34ba272f0b42ef1f918064f074c6', `point`='event:b16c7356168c168c4139c816ed13c0875f5d73cfe433921cf0e132ee0beeb43a', `role`='second', `side`='single', `source`='modules/vector-sets/hnsw.c:2032-2032'

Uncertain facts:

- `decisive`=False, `detail`='a recursive mutex (PTHREAD_MUTEX_RECURSIVE) makes a second acquisition by the same thread legal', `fact`='MUTEX_RECURSIVE', `status`='ambiguous'
- `decisive`=True, `detail`="the lock's name is the root of a longer expression, so two different locks can carry the same name", `fact`='SUBJECT_PATH', `point`='event:cd0714a64a8a24e02e41e874677e25c76dd62e2ef3f798a360d6a9ae3afb137d', `source`='modules/vector-sets/hnsw.c:2029-2029', `status`='ambiguous'

Missing evidence:

- whether the mutex is recursive: under PTHREAD_MUTEX_RECURSIVE a second acquisition by the same thread is legal, and the graph does not hold the mutex's type
- the execution context: whether the two methods can run concurrently at all -- nothing in the graph orders them
- lock aliasing: two expressions naming the same mutex (`&s->m` and `s->m`) are two names here, and one name is one lock
- the failure path of a non-blocking acquisition: a try-lock that fails leaves the lock unheld and the graph cannot tell the two arms apart

```c

    /* Try to lock the selected slot. */
    if (pthread_mutex_lock(&index->slot_locks[slot]) != 0) return -1;

    /* Get read lock. */
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
- `failure_reason`: `identity`
- `needs_dfg`: `false`
- `missing_capability`: `锁身份：subject_exact=false（根名不是那个对象）`
- `confidence`: `0.95`
- `reason`: `` modules/vector-sets/hnsw.c:2029 `pthread_mutex_lock(&index->slot_locks[slot])` 与 2032 `pthread_rwlock_rdlock(&index->global_lock)` 是两把不同的锁。 ``
- `annotator`: `AI 提议 + 人工确认（Claude Code 提议 / Kyber5323 逐条确认，2026-09-22）`
- `notes`: `null`

### DFV1-llamacpp100-71e4f44087696b32

- type: `4.1` / `resource_lifetime`
- status: `resolved` (confidence 0.6)
- subject: `chunk` in `mtmd_input_chunk_load`
- candidate: `E-DEFECT-4101ee2c700775d2e42b981e`
- anchor: `event:4507066100375e15856aaa71a3ec12a07f837654af407f73c635161cf71d55d3`
- source: `tools/mtmd/mtmd.cpp:2443-2443`, `tools/mtmd/mtmd.cpp:2444-2444`, `tools/mtmd/mtmd.cpp:2445-2445`

Facts:

- `event`='ALLOC', `matched_name`='new_expression', `method`='mtmd_input_chunk_load', `point`='event:4507066100375e15856aaa71a3ec12a07f837654af407f73c635161cf71d55d3', `source`='tools/mtmd/mtmd.cpp:2443-2443', `subject`='chunk', `subject_from`='assignment'
- `event`='DEREFERENCE', `matched_name`='chunk->deserialize', `method`='mtmd_input_chunk_load', `point`='event:41fe045437eb4192582e8cd6e4ec7c53611e6ab65fe377d8f93c2e3608bf42cf', `source`='tools/mtmd/mtmd.cpp:2444-2444', `subject`='chunk', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='mtmd_input_chunk_load', `point`='event:07f3e7807f691c574289eb7c1a752dbee589a3a5e552a3ac795c9991729664a8', `source`='tools/mtmd/mtmd.cpp:2444-2444', `subject`='ser', `subject_from`='argument'
- `event`='CALL', `matched_name`='call_expression', `method`='mtmd_input_chunk_load', `point`='event:b72235dc98d05dcc9c3eb1fc0e4da709721b9307203448f903a65aa106b5696b', `source`='tools/mtmd/mtmd.cpp:2445-2445', `subject`='chunk', `subject_from`='receiver'
- `event`='RETURN', `matched_name`='return_statement', `method`='mtmd_input_chunk_load', `point`='event:8dea9a58de75747c39d2fa10ed15c4c72a4225b5eca38eaa8f821f7cc65c00ff', `source`='tools/mtmd/mtmd.cpp:2445-2445', `subject`='', `subject_from`=''

Uncertain facts:


Missing evidence:

- the ownership contract of a callee: whether a function the resource is passed to takes ownership, borrows it, or frees it
- RAII and smart pointers: `unique_ptr<T> p(new T)` is an ALLOC with no RELEASE in the graph, so every owning smart pointer reads as a leak
- aliasing and reassignment: `q = p; free(q)`, `free(p); p = NULL` and `p = realloc(p, n)` are all invisible without a data-flow graph
- a release performed inside a callee: the graph only sees RELEASE points in the method it walked

```cpp
    try {
        mtmd_serialization ser(MTMD_SERIALIZATION_VERSION, buf, len);
        mtmd::input_chunk_ptr chunk(new mtmd_input_chunk());
        chunk->deserialize(ser);
        return chunk.release();
```

```cpp
        mtmd_serialization ser(MTMD_SERIALIZATION_VERSION, buf, len);
        mtmd::input_chunk_ptr chunk(new mtmd_input_chunk());
        chunk->deserialize(ser);
        return chunk.release();
    } catch (const std::exception & e) {
```

```cpp
        mtmd::input_chunk_ptr chunk(new mtmd_input_chunk());
        chunk->deserialize(ser);
        return chunk.release();
    } catch (const std::exception & e) {
        LOG_ERR("%s: %s\n", __func__, e.what());
```

Verdict (recorded in `defect_review.json`):

- `verdict`: `REVIEWED`
- `is_true_positive`: `false` — **false positive**
- `failure_reason`: `ownership`
- `needs_dfg`: `false`
- `missing_capability`: `RAII + 显式 release()`
- `confidence`: `0.95`
- `reason`: `` `mtmd::input_chunk_ptr` 是 `std::unique_ptr`（tools/mtmd/mtmd.h:483），2443 构造、2446 `return chunk.release();` 显式交出所有权。 ``
- `annotator`: `AI 提议 + 人工确认（Claude Code 提议 / Kyber5323 逐条确认，2026-09-22）`
- `notes`: `null`

### DFV1-redis50-74e583e4cf1eea15

- type: `4.1` / `resource_lifetime`
- status: `ambiguous` (confidence 0.4)
- subject: `tmp` in `spt_init`
- candidate: `E-DEFECT-ebcaf3f9c0975cc554e114e9`
- anchor: `event:64ee9372f711e6c2c7aa1efe56eddd09c16141af7e3112b3d05ecba06a225a10`
- source: `src/setproctitle.c:241-241`, `src/setproctitle.c:246-246`, `src/setproctitle.c:249-249`, `src/setproctitle.c:253-253`, `src/setproctitle.c:256-256`, `src/setproctitle.c:263-263`

Facts:

- `event`='ALLOC', `matched_name`='strdup', `method`='spt_init', `point`='event:64ee9372f711e6c2c7aa1efe56eddd09c16141af7e3112b3d05ecba06a225a10', `source`='src/setproctitle.c:241-241', `subject`='tmp', `subject_from`='assignment'
- `event`='CALL', `matched_name`='call_expression', `method`='spt_init', `point`='event:78395f4387daf72c18691f50eef7808600f8908b02df2493f08b8557ecb17ba5', `source`='src/setproctitle.c:241-241', `subject`='tmp', `subject_from`='assignment'
- `event`='CHECK', `matched_name`='!(tmp=strdup(program_invocation_short_name))', `method`='spt_init', `point`='event:240b6f4a8c2ec856b44d8f2b509224eb840d58f4955c347c8a2e071155e44b60', `source`='src/setproctitle.c:241-241', `subject`='', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='spt_init', `point`='event:49f22c8c2887f2d897c455c21e93a3f7bd5e44430b22eee0cbbe99333ce25e7f', `source`='src/setproctitle.c:246-246', `subject`='tmp', `subject_from`='assignment'
- `event`='ALLOC', `matched_name`='strdup', `method`='spt_init', `point`='event:2c40b1d2f5af7fbecb9e9729e132cbd1364dfdd13cd4b3bc6bdb16c55cc36642', `source`='src/setproctitle.c:246-246', `subject`='tmp', `subject_from`='assignment'
- `event`='CALL', `matched_name`='call_expression', `method`='spt_init', `point`='event:f8b528550ff4f5e96132e1a63c80afaafc4055c73452e344ea6d39e7ea2dba3c', `source`='src/setproctitle.c:246-246', `subject`='tmp', `subject_from`='assignment'
- `event`='CHECK', `matched_name`='!(tmp=strdup(getprogname()))', `method`='spt_init', `point`='event:6f7e27170785fc9c5e900d3adce4c02b4b6f1ebf60598d52dca5028e435b29d0', `source`='src/setproctitle.c:246-246', `subject`='', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='spt_init', `point`='event:2c95509c4395a5768621ddeeaaf00e71ee772666a16a7e69bea38f03309e9f81', `source`='src/setproctitle.c:249-249', `subject`='tmp', `subject_from`='argument'
- `event`='CALL', `matched_name`='call_expression', `method`='spt_init', `point`='event:705bf055ab5d7d832052e8cf90781af22215718a4d2a4cbf1ad4ea84736996e6', `source`='src/setproctitle.c:253-253', `subject`='error', `subject_from`='assignment'
- `event`='BRANCH', `matched_name`='(error=spt_copyenv(envc,envp))', `method`='spt_init', `point`='event:238232cb05bcfdb3c1e8bb160541da2246eda396571f8246a1c44dd5ab9fa282', `source`='src/setproctitle.c:253-253', `subject`='', `subject_from`=''
- `event`='CALL', `matched_name`='call_expression', `method`='spt_init', `point`='event:a4d2366c20d4cadee8490e8a22277c74066078fde5e5c2de868cba297c093b9e', `source`='src/setproctitle.c:256-256', `subject`='error', `subject_from`='assignment'
- `event`='BRANCH', `matched_name`='(error=spt_copyargs(argc,argv))', `method`='spt_init', `point`='event:8ce857042a848274c191faabe577cb2c402aee2f6458c8069e10fcbd0aa9cc76', `source`='src/setproctitle.c:256-256', `subject`='', `subject_from`=''
- `event`='RETURN', `matched_name`='return_statement', `method`='spt_init', `point`='event:ae56f0a8469d2ecebb057ba9190bd886a54ed09cfa3ecc866f5bc4d54a5b11f8', `source`='src/setproctitle.c:263-263', `subject`='', `subject_from`=''

Uncertain facts:

- `decisive`=True, `detail`='this name is passed to a callee, which may take ownership', `fact`='OWNERSHIP_TRANSFER', `point`='event:2c95509c4395a5768621ddeeaaf00e71ee772666a16a7e69bea38f03309e9f81', `source`='src/setproctitle.c:249-249', `status`='ambiguous'

Missing evidence:

- the ownership contract of a callee: whether a function the resource is passed to takes ownership, borrows it, or frees it
- RAII and smart pointers: `unique_ptr<T> p(new T)` is an ALLOC with no RELEASE in the graph, so every owning smart pointer reads as a leak
- aliasing and reassignment: `q = p; free(q)`, `free(p); p = NULL` and `p = realloc(p, n)` are all invisible without a data-flow graph
- a release performed inside a callee: the graph only sees RELEASE points in the method it walked

```c
	program_invocation_name = tmp;

	if (!(tmp = strdup(program_invocation_short_name)))
		goto syerr;

```

```c
	program_invocation_short_name = tmp;
#elif __APPLE__
	if (!(tmp = strdup(getprogname())))
		goto syerr;

```

```c
		goto syerr;

	setprogname(tmp);
#endif

```

```c

    /* Now make a full deep copy of the environment and argv[] */
	if ((error = spt_copyenv(envc, envp)))
		goto error;

```

```c
		goto error;

	if ((error = spt_copyargs(argc, argv)))
		goto error;

```

```c
	SPT.end  = end;

	return;
syerr:
	error = errno;
```

Verdict (recorded in `defect_review.json`):

- `verdict`: `REVIEWED`
- `is_true_positive`: `false` — **false positive**
- `failure_reason`: `ownership`
- `needs_dfg`: `false`
- `missing_capability`: `存储期：写入全局`
- `confidence`: `0.85`
- `reason`: `` `tmp = strdup(program_invocation_short_name)`（src/setproctitle.c:241）随后写进全局 `program_invocation_short_name`（243），进程存活期持有。 ``
- `annotator`: `AI 提议 + 人工确认（Claude Code 提议 / Kyber5323 逐条确认，2026-09-22）`
- `notes`: `判 FP 的依据是「设计如此」（进程/文件作用域）；若认为意图类判断不该进分母，应填 uncertain。`

### DFV1-llamacpp100-af4c18a0bead7112

- type: `4.1` / `resource_lifetime`
- status: `unresolved` (confidence 0.2)
- subject: `` in `llama_model_mapping`
- candidate: `E-DEFECT-0c47dc159ab6c88ca6eae535`
- anchor: `event:5482ac7b52971a956bfa64ed409002c94a8e4de1bdccfb6df19346e1f28ced9f`
- source: `src/llama-model.cpp:268-268`

Facts:

- `event`='ALLOC', `matched_name`='new_expression', `method`='llama_model_mapping', `point`='event:5482ac7b52971a956bfa64ed409002c94a8e4de1bdccfb6df19346e1f28ced9f', `source`='src/llama-model.cpp:268-268', `subject`='', `subject_from`=''
- `event`='RETURN', `matched_name`='return_statement', `method`='llama_model_mapping', `point`='event:ee22e03df78e15165a8f35dcc80bb7dd418117a297145031bff8ff3ab5e6bc11', `source`='src/llama-model.cpp:268-268', `subject`='', `subject_from`=''

Uncertain facts:

- `decisive`=True, `detail`='the acquisition names no target, so the candidate cannot be attributed', `fact`='SUBJECT_UNRESOLVED', `status`='ambiguous'

Missing evidence:

- the ownership contract of a callee: whether a function the resource is passed to takes ownership, borrows it, or frees it
- RAII and smart pointers: `unique_ptr<T> p(new T)` is an ALLOC with no RELEASE in the graph, so every owning smart pointer reads as a leak
- aliasing and reassignment: `q = p; free(q)`, `free(p); p = NULL` and `p = realloc(p, n)` are all invisible without a data-flow graph
- a release performed inside a callee: the graph only sees RELEASE points in the method it walked

```cpp
            return new llama_model_bailingmoe2(params);
        case LLM_ARCH_BAILINGMOE3:
            return new llama_model_bailingmoe3(params);
        case LLM_ARCH_SEED_OSS:
            return new llama_model_seed_oss(params);
```

Verdict (recorded in `defect_review.json`):

- `verdict`: `REVIEWED`
- `is_true_positive`: `false` — **false positive**
- `failure_reason`: `ownership`
- `needs_dfg`: `false`
- `missing_capability`: `所有权：返回值/工厂`
- `confidence`: `0.9`
- `reason`: `` src/llama-model.cpp:266 `return new llama_model_bailingmoe2(params);`，整个工厂 switch 的每个分支都是同一形状（255-272），返回值即所有权交接。 ``
- `annotator`: `AI 提议 + 人工确认（Claude Code 提议 / Kyber5323 逐条确认，2026-09-22）`
- `notes`: `null`

### DFV1-redis50-cf63d8fb0c8ef73b

- type: `4.1` / `resource_lifetime`
- status: `resolved` (confidence 0.6)
- subject: `tmp` in `spt_clearenv`
- candidate: `E-DEFECT-5b6cd801327af1e58e759032`
- anchor: `event:105db58a952be41bff0b02ba0e4a2c1b4868b75e1f6790d56bc5ed0620a7ddaf`
- source: `src/setproctitle.c:94-94`, `src/setproctitle.c:95-95`, `src/setproctitle.c:97-97`, `src/setproctitle.c:100-100`

Facts:

- `event`='ALLOC', `matched_name`='malloc', `method`='spt_clearenv', `point`='event:105db58a952be41bff0b02ba0e4a2c1b4868b75e1f6790d56bc5ed0620a7ddaf', `source`='src/setproctitle.c:94-94', `subject`='tmp', `subject_from`='assignment'
- `event`='CALL', `matched_name`='call_expression', `method`='spt_clearenv', `point`='event:0804f9308333e4ca88ffffd4f3a1c3125884c3bd91bc5e5cab5e62db3a5cb90c', `source`='src/setproctitle.c:94-94', `subject`='tmp', `subject_from`='assignment'
- `event`='CHECK', `matched_name`='!(tmp=malloc(sizeof*tmp))', `method`='spt_clearenv', `point`='event:b4440360f70eb1814584cb9f6faab0aa68d3f5af7d7c732566ccc8b4ad45eeb0', `source`='src/setproctitle.c:94-94', `subject`='', `subject_from`=''
- `event`='RETURN', `matched_name`='return_statement', `method`='spt_clearenv', `point`='event:aab288aa4e26b7bb2a917a6419b2b70dd970f2c02176ff1c7c7ac4170af6aa5a', `source`='src/setproctitle.c:95-95', `subject`='errno', `subject_from`='return'
- `event`='DEREFERENCE', `matched_name`='tmp[0]', `method`='spt_clearenv', `point`='event:4eb009c8c3efc5e20cfe17804ae8fb8c212c67be3b7471dc258d6f244e75cf36', `source`='src/setproctitle.c:97-97', `subject`='tmp', `subject_from`=''
- `event`='RETURN', `matched_name`='return_statement', `method`='spt_clearenv', `point`='event:279a576670b26fc2327a4ca4cbfb7f9170223e3e5c100e4a1e2ce085614ef1a3', `source`='src/setproctitle.c:100-100', `subject`='', `subject_from`=''

Uncertain facts:


Missing evidence:

- the ownership contract of a callee: whether a function the resource is passed to takes ownership, borrows it, or frees it
- RAII and smart pointers: `unique_ptr<T> p(new T)` is an ALLOC with no RELEASE in the graph, so every owning smart pointer reads as a leak
- aliasing and reassignment: `q = p; free(q)`, `free(p); p = NULL` and `p = realloc(p, n)` are all invisible without a data-flow graph
- a release performed inside a callee: the graph only sees RELEASE points in the method it walked

```c
	static char **tmp;

	if (!(tmp = malloc(sizeof *tmp)))
		return errno;

```

```c

	if (!(tmp = malloc(sizeof *tmp)))
		return errno;

	tmp[0]  = NULL;
```

```c
		return errno;

	tmp[0]  = NULL;
	environ = tmp;

```

```c
	environ = tmp;

	return 0;
#endif
} /* spt_clearenv() */
```

Verdict (recorded in `defect_review.json`):

- `verdict`: `REVIEWED`
- `is_true_positive`: `false` — **false positive**
- `failure_reason`: `ownership`
- `needs_dfg`: `false`
- `missing_capability`: `存储期：进程生命周期（environ）`
- `confidence`: `0.85`
- `reason`: `` src/setproctitle.c:92-98 `static char **tmp; tmp = malloc(...); environ = tmp;`——这块内存必须活到进程结束，释放它才是 bug。 ``
- `annotator`: `AI 提议 + 人工确认（Claude Code 提议 / Kyber5323 逐条确认，2026-09-22）`
- `notes`: `判 FP 的依据是「设计如此」（进程/文件作用域）；若认为意图类判断不该进分母，应填 uncertain。`
