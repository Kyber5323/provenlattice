# 03 · 稀疏 CFG 与稀疏 DFG

> 本篇回答：这两种图各自保留什么、压缩掉什么？五类数据流是什么？稀疏到什么程度会出问题？
> 依据：设计文档 §6 / §7，`sparsecfg.py` / `sparsedfg.py`，以及冻结库实测。

---

## 一、Sparse CFG：只留关键事件之间的可达性

### 设计意图（设计文档 §6 的例子）

```cpp
p = malloc(1024);
do_work();

if (error) {
    log_error();
    return;
}

cleanup_temp();
free(p);
return;
```

**完整 CFG** 会包含 `do_work()`、`log_error()`、`cleanup_temp()` 这些普通语句节点。

**稀疏 CFG** 只保留：

```
ALLOC(p)
   │
   ▼
BRANCH(error)
  /          \
true         false
 │             │
RETURN      RELEASE(p)
                │
                ▼
              RETURN
```

### 关系的定义

```
CONTROL_REACHES(A, B)
```

> 从关键语义事件 A 出发，控制流**可以到达**关键语义事件 B，**中间允许经过被压缩掉的普通语句**。

**怎么读**：这句话里最关键的是「**中间允许经过**」。`CONTROL_REACHES` **不是**「A 的下一步是 B」，而是「A 能走到 B，中间的东西我没存」。

**这带来一个必须记住的性质**：`CONTROL_REACHES` **不是传递闭包**。A→B 和 B→C 都存在时，A→C 可能**不**存在（如果 A→C 的直接可达性没被单独发布）。任何把它当闭包用的推理都是错的。

### 边上的 flags

| flag | 含义 |
|---|---|
| `branch=true` / `branch=false` | 该边是某个条件的真/假臂 |
| `loop_back` | 回边 |
| `fallthrough` | switch 链的落空（设计文档 §6 之外新增） |
| `exception` | 异常路径 |

**实测分布**（redis-50，162,268 条 `CONTROL_REACHES`）：

| flags | 条数 | 占比 |
|---|---:|---:|
| `[]`（无标志） | 108,242 | 66.7% |
| `["branch=true"]` | 24,550 | 15.1% |
| `["branch=false"]` | 23,869 | 14.7% |
| `["loop_back"]` | 2,975 | 1.8% |
| 带 `loop_back` 的分支边 | 2,498 | 1.5% |
| `fallthrough` 系列 | 134 | 0.08% |

**`exception` 标志的跨仓对比**：

| 语料 | `exception` 边 | 占控制边 |
|---|---:|---:|
| redis-50（C） | **0** | 0% |
| libgit2-24（C） | **0** | 0% |
| llama.cpp-69（C++） | **1,182** | 0.28% |

**怎么读**：三件事。

1. **三分之二的边没有任何标志**——就是「顺序执行」。
2. **分支边合计约 30%**，这是控制流推理的全部素材。
3. **`exception` 只在 C++ 语料上出现。** 两个纯 C 仓都是 0，llama.cpp 有 1,182 条。这说明该标志**不是未兑现，而是语言相关**——C 没有异常，所以没有异常边。**这条对比同时是一个反例警示：任何只在单仓上测出的「某功能没工作」结论，都必须跨仓复核。**

**一个 flags 的细节**：flags 是**多重集**，不是布尔组——`fallthrough` 会在同一条边上**重复**出现，重复次数 = switch 链跨越的层数（实测最长的拼写是 `branch=true+fallthrough×5`）。**读 flags 时不能当成 set**，否则会丢掉层数信息。

### 合成 EXIT 端点

CFG 的边需要一个终点。方法出口是**合成**的——`cfg_exit_id(repo_id, owner_id)`，**没有对应的事件行**。

**怎么读**：这解释了为什么 `semantic_edges` 里会有指向「不存在的事件」的边。这不是数据损坏，是设计。任何按 `dst_event_id` 做 JOIN 的查询都要处理这个空端。

---

## 二、Sparse DFG：只留五类缺陷相关的流

### 设计意图（设计文档 §7 的例子）

```cpp
char *name = request->name;
char *tmp = name;
sprintf(buffer, tmp);
```

**完整 DFG** 是 `request->name → name → tmp → argument → sprintf`（5 个节点）。

**稀疏 DFG** 压成一条边：

```
ExternalInput:request.name ──DATA_FLOW_TO──▶ Call:sprintf
```

> 核心原则：**数据流边优先于中间表达式节点。**
> 中间传播链作为 evidence/path metadata 保存，需要解释时再恢复。

**实现**：中间链存在边的 `via` 元数据里，可以恢复出完整路径用于解释。**图里不存，证据里有。**

### def / use 的定义（`sparsedfg.py:66-67`）

```python
DEF_EVENTS = frozenset({"WRITE", "ALLOC"})
USE_EVENTS = frozenset({"READ", "DEREFERENCE", "CHECK", "RETURN"})
```

**两条必须注意的边界**：

- **`RELEASE` 既不是普通 def 也不是 use**。它开启「已释放」状态链，并杀掉活跃的 `ALLOC`。
- **`CALL` 不在 use 里**。这就是「`free(p); sink(p)` 不产边」的根因（见 §四）。

### 五类流（实测计数）

| flow_class | 含义 | libgit2-24 | redis-50 | llama.cpp-69 |
|---|---|---:|---:|---:|
| `input_to_sink` | def → **CALL 实参根** | 2,988 | 3,491 | 5,507 |
| `value_flow` | 非 alloc 的裸 WRITE → use（**残类**） | 2,234 | 2,461 | 4,956 |
| `null_to_deref` | `null_source` 的 def → `DEREFERENCE` | 136 | 52 | 8 |
| `alloc_to_use` | `ALLOC` 的 def → use | 62 | 40 | 750 |
| `free_to_use` | `RELEASE` 的 def → use | 14 | 11 | 47 |
| **合计 `DATA_FLOW_TO`** | | **5,434** | **6,055** | **11,268** |

**怎么读**：这张表是**后面所有 DFG 类查询的真实分母**，必须看懂。

- **`input_to_sink` 与 `value_flow` 占了 95% 以上**，它们是宽泛的「有值流过去」关系。
- **三类真正与缺陷判据直接对应的流非常小**：`null_to_deref`（52）、`alloc_to_use`（40）、`free_to_use`（11）——redis 上合计 **103 条**。
- **空指针 / 释放后使用 / 双释放这三个查询，就是在 103 条边上工作的。** 这直接解释了为什么它们的候选数天然是几百量级而非几万。

**一个重要例外**：`alloc_to_use` 在 llama 上是 750 条（redis 只有 40）——**差 19 倍**。原因是 llama 大量使用 C++ `new`，而 `new_expression` 是词汇表里的 `ALLOC` 语法。**同一套规则在不同语料上的产出结构差异极大**，这是本项目反复强调「数字不可跨仓外推」的又一个实例。

### 分类优先级（`sparsedfg.py:284-291`）

一条边同时符合多个描述时，按固定优先级取一个标签：

```
free 源        → free_to_use
否则 null 源且 use 是 DEREFERENCE → null_to_deref
否则 alloc 源  → alloc_to_use
否则           → value_flow（残类）
```

**关键机制**：`_root_def` 沿 `alias_parent` 上溯 ⇒ **别名链已折叠进 flow_class**。`p = NULL; q = p; *q` 会正确落进 `null_to_deref`，而不是因为用了 `q` 就漏掉。

---

## 三、稀疏到什么程度会出问题：`live_free` 的教训

**`free_to_use` 是路径盲的。**

`live_free` 只被**后来的 RELEASE 替换**，**从不被清除**。这意味着 `free_to_use` 的边是**按源码顺序**铸出来的，**与控制流路径无关**。

**实测后果**：

| 语料 | `free_to_use` 边 | 在 CFG 上真的可达 |
|---|---:|---:|
| redis-50 | 11 | **0** |
| llama.cpp-69 | 47 | **1** |

**怎么读**：**redis 上 11 条边，11 条在控制流上不相连。** 源码复核证实两例都是 `free(x); return;` 形式的清理路径——`free` 在一条会 return 的臂上，`use` 在另一条臂上。**它们不是 UAF。**

**这是本项目最重要的一条自我登记**：`free_to_use` **不是** UAF 证据，它是**候选级关系**。可达性是唯一能把它和真 UAF 分开的判据。

**这条发现的代价与价值**：它是在阶段 6 的冻结探针里发现的，**推翻了一版设计**（原计划直接消费 `free_to_use` 边，实测会被 11/11 全假阳性污染）。发现方式是可证伪纪律：探针在实现之前先跑，跑出来不对，改设计而不是改判据。

---

## 四、DFG 不产边的四种形态（真实边界）

下面四种形态**不产生任何边**。它们不是 bug，是稀疏 DFG 的已知边界：

| # | 形态 | 为什么不产边 |
|---|---|---|
| 1 | `free(p); sink(p)` | `RELEASE` 删掉 `live_root[subject]`，而 **`CALL` 不在 `USE_EVENTS` 里** ⇒ 查不到源 |
| 2 | `free(p); p = NULL; *p` | 落进 `null_to_deref` 而非 `free_to_use`（USE 分支取更晚的 def）——**两个查询因此天然分区**，不重叠 |
| 3 | `p = 0` / `p == 0` | `null_node_types` 只认字面量 `null`（继承登记，未翻案） |
| 4 | `free(p); free(q)`（q 别名 p） | 没有 release→release 关系——**双释放查询因此不消费 DFG**，改按身份分组配对 |

**怎么读**：第 1 条在阶段 6 有了部分补偿——`use_after_free` 查询在**查询期**组合 CALL 作为 USE（扫同方法的 CALL 事件、解析实参根、要求位置在 RELEASE 之后且其间无同身份 def）。**但 DFG 本身仍然不铸这条边。**

**这是「查询期补偿」与「图内事实」的分界**：图不撒谎（不铸没把握的边），查询层做有明确条件的组合。代价是这类候选**恒为 `ambiguous`**——因为 `_root_identifier` 会剥掉 `&`，`f(p)` 与 `f(&p)` 在图上不可区分。

---

## 五、一个必须记住的语义陷阱：WRITE 源是 CFG sink

**这是本项目踩过的最大的坑，也是最有价值的一条经验。**

DFG 的 def 有两类来源：`WRITE` 与 `ALLOC`。它们在图里的形态**完全不同**：

| def 来源 | 在 CFG 里的形态 | `control_reachable` 标志 |
|---|---|---|
| `ALLOC` | **有出边** | 真值，可用 |
| `WRITE` | **sink（无出边）** | **恒为 `false`** |

**`WRITE` 点不在任何控制流边里**（`_EDGELESS_EVENT_TYPES`）。

**后果**：如果把 `control_reachable == false` 直接读成「不可达」，会——

> **杀掉全部 52/52 条候选。**

**正确读法（三态）**：

| 情形 | 含义 | 处置 |
|---|---|---|
| 源**有出边** ∧ 不可达 | 真的不相连 | **淘汰** |
| 源是 **sink** | **「没有位置可算」**，不是「不可达」 | **未知** ⇒ 降级 `ambiguous`，**不淘汰** |

**怎么读**：这不是实现细节，是**语义契约**。同一个 `false` 值在不同源类型上含义不同，读错就会静默清空候选——**而且不会报错**。

源码里对此有专门的自述（`defect.py:2924-2932`）：「这个点在图里吗」对 WRITE 点恒答否，读成「不可达」会**结构性删掉发现**——llama 上那是 47 条 `free_to_use` 里的 27 条。

---

## 六、`find_paths` 不是可达性判定器

这是第二个必须记住的坑。

`find_paths` 是一个**路径枚举器**，不是可达性判定器。它给每个队列项**自带一个 visited 集**，为的是「环路两侧的两条不同路径是两个事实」——这对产出证据路径是对的，对判定可达性是灾难：

> 用 `max_hops=4096` 调 `find_paths` 做不可达判定会**指数爆炸**——实测 **120 秒不返回**。

**正确分工**：

| 用途 | 工具 |
|---|---|
| 判定可达性（淘汰候选） | **节点可达性 BFS**（每源一趟 O(V+E)，可记忆化，任意深度精确） |
| 产出候选的证据路径 | `find_paths`（`max_hops` 取小值 + `max_paths` 封顶） |

**这条教训的实测代价**：阶段 6 的 `error_handling` 查询原经 `find_paths` 做窗口判决，在 redis 上遇到 3,989 个循环窗口后**超过 600 秒 / 13.7 GB 内存不返回**。修复方式是从分支扫描自己的 BFS 树里读出路线（零额外成本、与判决同源），修后 **1.6 秒**，候选与冻结预测**逐位一致**（见 [06](06-实验结果.md) §八）。

---

**上一篇** → [02-架构与数据模型](02-架构与数据模型.md)
**下一篇** → [04-缺陷查询与证据包](04-缺陷查询与证据包.md)：这些图怎么被查询消费。
