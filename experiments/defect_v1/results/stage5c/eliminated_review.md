# Stage 5C — Eliminated Candidates: Adjudication Sheet

Protocol: a stratified 12-sample of the candidates the 5C rulings eliminated,
drawn from the post-5A/5C-2 rebuilds. Strata are the ruling reasons, taken
round-robin over records ordered by (repository, span, subject) — one reason
cannot eat the quota. The assistant proposes a verdict per sample after
reading the snippet; **the user rules, and the sheet is only as good as
those rulings.**

Elimination pool: {'DECLARED_CONSUMES': 0, 'ESCAPE_RETURN': 173, 'ESCAPE_TO_SHARED': 8, 'INFERRED_CONSUMES': 0, 'PAIR_CALLER_OWNS': 4, 'RAII_WRAPPER_TYPE': 18, 'RETURN_TRANSFER': 84, 'STORAGE_OWNER': 21, 'STORAGE_OWNER_FUNCTION_STATIC': 12}

Databases:

- `redis50`: `d:/tmp/stage5c/redis50-5c2.db`
- `llamacpp100`: `d:/tmp/stage5c/llama69-5c2.db`

---

## ELIM-01 — DECLARED_CONSUMES (declared:consumes:RedisModule_SetKeyMeta)

- repository: `redis50`  defect key: 4.1
- method: `HashNotifyCallback`  subject: `new_str`  storage: `local`  type: `char`
- span: `tests/modules/keymeta_notify.c:55-55`
- ruling: every path passes this name to RedisModule_SetKeyMeta as argument 2, which takes ownership of it

`tests/modules/keymeta_notify.c` (from line 53):

```c

    /* Set new metadata - a simple string "notified" */
    char *new_str = strdup("notified");
    if (RedisModule_SetKeyMeta(meta_class_id, k, (uint64_t)new_str) == REDISMODULE_OK) {
        meta_set_count++;
```

**Proposed verdict: agree** — the strdup'd string is handed to RedisModule_SetKeyMeta, whose declared contract takes ownership of argument 2; the uint64_t cast is that API's handle convention, not a lost pointer

**User ruling:** ____________同意__________

---

## ELIM-02 — ESCAPE_RETURN (graph:alloc_return)

- repository: `llamacpp100`  defect key: 4.1
- method: `common_log_init`  subject: ``  storage: ``  type: ``
- span: `common/log.cpp:450-450`
- ruling: the allocation is the method's return value, so it leaves this frame as it is acquired

`common/log.cpp` (from line 448):

```c

struct common_log * common_log_init() {
    return new common_log;
}

```

**Proposed verdict: agree** — the anonymous new is the method's return value, so ownership leaves with the caller on every path that contains it

**User ruling:** _______同意_______________

---

## ELIM-03 — ESCAPE_TO_SHARED (graph:escape_write)

- repository: `llamacpp100`  defect key: 4.1
- method: `iq2xs_init_impl`  subject: `the_grid`  storage: `local`  type: `uint64_t`
- span: `ggml/src/ggml-quants.c:3119-3119`
- ruling: the allocation's pointer is stored into iq2_data by plain assignment, which outlives this frame, and every leak path passes that write

`ggml/src/ggml-quants.c` (from line 3117):

```c

    //printf("================================================================= %s(grid_size = %d)\n", __func__, grid_size);
    uint64_t * the_grid = (uint64_t *)malloc(grid_size*sizeof(uint64_t));
    for (int k = 0; k < grid_size; ++k) {
        int8_t * pos = (int8_t *)(the_grid + k);
```

**Proposed verdict: agree** — the_grid is stored into the file-static iq2_data[gindex].grid and the global kgrid_q2xs by plain assignment, so the object outlives the frame; the writes are straight-line in this initializer

**User ruling:** _______同意_______________

---

## ELIM-04 — INFERRED_CONSUMES (inferred:consumes:gguf_free)

- repository: `llamacpp100`  defect key: 4.1
- method: `gguf_init_from_reader`  subject: `ctx`  storage: `local`  type: `gguf_context`
- span: `ggml/src/gguf.cpp:460-460`
- ruling: every path passes this name to gguf_free as argument 0, which takes ownership of it

`ggml/src/gguf.cpp` (from line 458):

```c

static struct gguf_context * gguf_init_from_reader(const struct gguf_reader & gr, struct gguf_init_params params) {
    struct gguf_context * ctx = new gguf_context;

    bool ok = true;
```

**Proposed verdict: agree** — no path leaks: every failure branch calls gguf_free(ctx) and the success path returns ctx to the caller. The recorded reason is narrower than the truth -- it holds on the walk's 8-path view (all failure paths); the success path's hand-over is the return, which this record does not cite

**User ruling:** ________同意______________

---

## ELIM-05 — PAIR_CALLER_OWNS (inferred:pair:hnsw_acquire_read_slot -> hnsw_release_read_slot)

- repository: `redis50`  defect key: 4.6
- method: `hnsw_acquire_read_slot`  subject: `index`  storage: `parameter`  type: `HNSW`
- span: `modules/vector-sets/hnsw.c:2016-2016`
- ruling: this method is the acquire half of an acquire/release pair, so it returns with the lock held by contract and the release belongs to the caller

`modules/vector-sets/hnsw.c` (from line 2014):

```c
    /* First try a non-blocking approach on all slots. */
    for (uint32_t i = 0; i < HNSW_MAX_THREADS; i++) {
        if (pthread_mutex_trylock(&index->slot_locks[i]) == 0) {
            if (pthread_rwlock_rdlock(&index->global_lock) != 0) {
                pthread_mutex_unlock(&index->slot_locks[i]);
```

**Proposed verdict: agree** — this is the acquire half of the acquire/release pair; the release obligation belongs to the caller by that contract, which is the stage-4 adjudicated reading

**User ruling:** ________同意______________

---

## ELIM-06 — RAII_WRAPPER_TYPE (declared:wrapper:common_chat_templates_ptr)

- repository: `llamacpp100`  defect key: 4.1
- method: `common_chat_templates_init`  subject: `tmpls`  storage: `local`  type: `common_chat_templates_ptr`
- span: `common/chat.cpp:835-835`
- ruling: the allocation is held in common_chat_templates_ptr, which frees it on every path out of the scope, including exception unwinding

`common/chat.cpp` (from line 833):

```c
        add_eos   = llama_vocab_get_add_eos(vocab);
    }
    common_chat_templates_ptr tmpls(new common_chat_templates());
    tmpls->has_explicit_template = has_explicit_template;
    tmpls->add_bos               = add_bos;
```

**Proposed verdict: agree** — the allocation is held by common_chat_templates_ptr, a registered RAII wrapper that frees on every scope exit including unwinding

**User ruling:** _______同意_______________

---

## ELIM-07 — RETURN_TRANSFER (graph:return)

- repository: `llamacpp100`  defect key: 4.1
- method: `common_sampler_init`  subject: `result`  storage: `local`  type: `auto`
- span: `common/sampling.cpp:427-435`
- ruling: every path out of this method returns the resource to the caller, so the release obligation leaves with it

`common/sampling.cpp` (from line 425):

```c
    }

    auto * result = new common_sampler {
        /* .params  = */ params,
        /* .grmr    = */ grmr,
        /* .rbudget = */ rbudget,
        /* .chain   = */ chain,
        /* .prev    = */ ring_buffer<llama_token>(std::max(32, params.n_prev)),
        /* .cur     = */ {},
        /* .cur_p   = */ {},
    };

    return result;
```

**Proposed verdict: agree** — the new common_sampler is assigned to result and result is returned; the release obligation leaves with the caller

**User ruling:** ________同意______________

---

## ELIM-08 — STORAGE_OWNER (graph:storage:field)

- repository: `llamacpp100`  defect key: 4.1
- method: `common_speculative_impl_draft_dflash.common_speculative_impl_draft_dflash`  subject: `batch_inject`  storage: `field`  type: `llama_batch`
- span: `common/speculative.cpp:1017-1017`
- ruling: the allocation is kept in a field slot, so it belongs to that owner rather than to this frame

`common/speculative.cpp` (from line 1015):

```c
        if (is_mrope) {
            free(batch_inject.pos);
            batch_inject.pos = (llama_pos *) malloc(sizeof(llama_pos) * 4 * llama_n_batch(ctx_dft));
        }

```

**Proposed verdict: agree** — batch_inject.pos is a field of the owning struct, and the snippet shows the paired free immediately before the re-allocation -- the object, not this frame, manages the slot

**User ruling:** ________同意______________

---

## ELIM-09 — STORAGE_OWNER_FUNCTION_STATIC (graph:storage:function_static)

- repository: `llamacpp100`  defect key: 4.1
- method: `jinja.value_string_t.get_builtins`  subject: `builtins`  storage: `function_static`  type: `func_builtins`
- span: `common/jinja/value.cpp:896-896`
- ruling: the allocation is kept in a function-static slot, which outlives this frame; whether a later call overwrites that slot without freeing it first is not visible from one method

`common/jinja/value.cpp` (from line 894):

```c
            std::istringstream iss = std::istringstream(input);
            std::string line;
            while (std::getline(iss, line)) {
                if (!indented.empty()) {
                    indented.push_back('\n');
```

**Proposed verdict: agree** — the map is a function-static that outlives the frame, and it is declared const, so the record's own overwrite caveat cannot even trigger. The snippet window shows a loop inside the initializer rather than the declaration; the ruling is storage-based, so it stands

**User ruling:** ______同意________________

---

## ELIM-10 — DECLARED_CONSUMES (declared:consumes:RedisModule_SetKeyMeta)

- repository: `redis50`  defect key: 4.1
- method: `PerKeyMetadataJob`  subject: `new_str`  storage: `local`  type: `char`
- span: `tests/modules/postnotifications_perkey_metadata.c:111-111`
- ruling: every path passes this name to RedisModule_SetKeyMeta as argument 2, which takes ownership of it

`tests/modules/postnotifications_perkey_metadata.c` (from line 109):

```c
    }

    char *new_str = strdup("notified");
    if (RedisModule_SetKeyMeta(meta_class_id, k, (uint64_t)new_str) == REDISMODULE_OK) {
        fire_count++;
```

**Proposed verdict: agree** — same shape as ELIM-01: handed to RedisModule_SetKeyMeta, which owns it by its declared contract

**User ruling:** ________同意______________

---

## ELIM-11 — ESCAPE_RETURN (graph:alloc_return)

- repository: `llamacpp100`  defect key: 4.1
- method: `common_sampler_clone`  subject: ``  storage: ``  type: ``
- span: `common/sampling.cpp:510-518`
- ruling: the allocation is the method's return value, so it leaves this frame as it is acquired

`common/sampling.cpp` (from line 508):

```c

struct common_sampler * common_sampler_clone(common_sampler * gsmpl) {
    return new common_sampler {
        /* .params  = */ gsmpl->params,
        /* .grmr    = */ llama_sampler_clone(gsmpl->grmr),
        /* .rbudget = */ llama_sampler_clone(gsmpl->rbudget),
        /* .chain   = */ llama_sampler_clone(gsmpl->chain),
        /* .prev    = */ gsmpl->prev,
        /* .cur     = */ gsmpl->cur,
        /* .cur_p   = */ gsmpl->cur_p,
    };
}

```

**Proposed verdict: agree** — the clone's anonymous new is the method's return value; ownership leaves with the caller

**User ruling:** ____________同意__________

---

## ELIM-12 — ESCAPE_TO_SHARED (graph:escape_write)

- repository: `llamacpp100`  defect key: 4.1
- method: `iq2xs_init_impl`  subject: `kmap_q2xs`  storage: `local`  type: `int`
- span: `ggml/src/ggml-quants.c:3129-3129`
- ruling: the allocation's pointer is stored into iq2_data by plain assignment, which outlives this frame, and every leak path passes that write

`ggml/src/ggml-quants.c` (from line 3127):

```c
    kgrid_q2xs = the_grid;
    iq2_data[gindex].grid = the_grid;
    kmap_q2xs = (int *)malloc(kmap_size*sizeof(int));
    iq2_data[gindex].map = kmap_q2xs;
    for (int i = 0; i < kmap_size; ++i) kmap_q2xs[i] = -1;
```

**Proposed verdict: agree** — kmap_q2xs is stored into the file-static iq2_data[gindex].map by plain assignment; same shape as ELIM-03

**User ruling:** ________同意______________

---
