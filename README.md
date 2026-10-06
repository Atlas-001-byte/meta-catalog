# Meta Catalog

元数据目录与检索服务：Schema 注册、影响分析、全文检索，以及字段级破坏性变更
识别与变更追溯检索。纯 Python 标准库实现，不依赖外部同类组件。

## 能力概览

1. **Schema 注册**：按 `名称@版本` 注册不可变 JSON Schema；支持跨 Schema
   `$ref`（形如 `Address@1.0#/properties/street`），允许前向引用与引用环，
   目标 Schema 已注册但引用字段不存在时注册即报错。
2. **目录资产**：注册直接引用某些 Schema 字段的资产（服务、任务、报表等）。
3. **影响分析**：给定字段，返回直接引用它的资产，以及经其他 Schema
   `$ref` 传递引用它的资产；传递影响按稳定路径去重。
4. **字段级变更识别**：比较同一 Schema 的基线版本与候选文档（可附显式字段
   重命名映射），生成可直接展示、可检索的字段级变更报告，报告关联原 Schema
   标识与版本。
5. **升级影响汇总**：在字段级变更报告之上按资产汇总升级影响
   （`analyze_upgrade_impact`），标注每个资产受影响的变更与命中方式
   （直接 / 传递 / 两者），只读执行，不生成报告、不入索引。
6. **全文检索**：Schema、资产与报告中的每条字段变更统一入索引，支持关键词
   与结构化条件组合检索。
7. **引用完整性审计**：`check_schema_references` 只读审计已注册 Schema 中的
   跨 Schema `$ref`，逐条标注 `resolved` / `missing_schema` /
   `missing_field` / `invalid_pointer`，不入索引、不改动注册内容。
8. **影响链路解释**：`explain_impact` 按资产解释指定 Schema 版本中逻辑字段
   的影响来源：直接命中给出零步链，传递命中给出沿跨 Schema `$ref` 的实际
   步进链；同一 `(source, target)` 只保留一条最短链，`impact_kind` 标注
   直接 / 传递 / 两者。只读执行，不注册资源、不生成报告、不入索引。
9. **变更预检**：`preview_changes(changes, *, search=None)` 对一批尚未提交
   的字段级变更做只读预检：不注册 Schema、不生成报告、不写索引，既有注册
   内容、报告库与检索行为完全不变。
10. **批量影响分析**：`analyze_impact_batch(request)` 按字段集合筛选受影响
    资产：`all` 要求命中每个字段，`any` 要求至少命中一个字段；可选资产标识
    列表限定候选范围。逐字段给出与单字段分析同口径的直接信息与传递引用
    证据。只读执行，不注册资源、不生成报告、不入索引。
11. **引用迁移规划**：`plan_reference_migration(name, baseline_version,
    candidate_version, *, renames=None)` 在两个已注册版本之间规划引用迁移：
    盘点指向基线版本的跨 Schema `$ref` 与资产直接引用，逐条给出
    `ready` / `renamed` / `broken` 与建议目标。只读执行，不注册资源、
    不生成报告、不入索引。

## 快速开始

```python
from meta_catalog import MetaCatalog
from meta_catalog.errors import SchemaComparisonInvalid, ImpactAnalysisTooLarge

catalog = MetaCatalog()

catalog.register_schema("Person", "1.0", {
    "type": "object",
    "properties": {
        "name":  {"type": "string"},
        "age":   {"type": "integer"},
        "level": {"type": "string", "enum": ["a", "b"]},
    },
    "required": ["name"],
})

catalog.register_asset("svc-order", "订单服务", "service", refs=[
    {"schema": "Person", "version": "1.0", "path": "/name"},
])

report = catalog.compare_schemas(
    "Person", "1.0",
    {
        "type": "object",
        "properties": {
            "name":  {"type": "string"},
            "years": {"type": "integer"},          # age 改名为 years
            "level": {"type": "string", "enum": ["a", "b", "c"]},  # 放宽枚举
            "nick":  {"type": "string"},           # 新增非必填
        },
        "required": ["name"],
    },
    candidate_version="2.0",
    renames=[{"from": "/age", "to": "/years"}],
)

# 读取 / 追溯检索
catalog.get_report(report["report_id"])
catalog.search(schema="Person", version="2.0", compatibility="breaking")
catalog.search(change_kind="rename", asset_name="订单")
```

## 字段路径

字段路径采用逻辑化 JSON Pointer：

- 对象属性省略 `properties` 段：`/properties/name` 写作 `/name`；
- 数组元素写作 `-`：`/properties/tags/items` 写作 `/tags/-`；
- `additionalProperties` 写作 `*`；
- 根对象为空串 `""`。

资产注册时既接受文档指针也接受逻辑路径，注册时统一结合文档结构解析。

## 变更种类与兼容结论

每条结果包含：字段路径、`old_path`/`new_path`、旧定义摘要、新定义摘要、
变更种类、兼容结论、直接影响资产、传递影响资产。

| 变更种类 `change_kind` | 含义 | 兼容结论 |
|---|---|---|
| `added` | 新增属性 | 非必填 `compatible`；必填 `breaking` |
| `deleted` | 删除属性 | `breaking` |
| `modified` | 结构定义变化 | 按下表判定 |
| `required_added` | 既有字段新增必填约束 | `breaking` |
| `required_removed` | 既有字段解除必填约束 | `compatible` |
| `rename` | 显式声明的字段重命名（整棵子树按一次计入） | `breaking` |
| `metadata` | 仅 `title`/`description`/`$comment` 变化 | `metadata` |

`modified` 的兼容规则：

- **兼容**：放宽枚举、`integer` 扩展为 `number`、增加默认值、取消/放宽数值与
  长度边界、扩大类型集合；
- **破坏**：收窄类型或枚举、新增枚举约束、新增边界约束、允许原本不接受的
  `null`、移除或改变默认值；
- 同时存在放宽与收窄时，按 **breaking** 处理。

显式重命名映射按一次 `rename` 计入结果，不再同时计为删除和新增；映射子树中
未被覆盖的删除与新增仍分别呈现。结构变化与新增必填同时发生时，种类记为
`modified`，兼容结论取更严格的 `breaking`。

## 重命名映射规则

`renames=[{"from": 旧路径, "to": 新路径}, ...]`，以下情形统一返回错误码
`SchemaComparisonInvalid`：

- 候选文档不是合法 JSON Schema；
- 指定的基线/候选版本不存在；
- 重命名起点在基线版本不存在，或终点在候选版本不存在；
- 同一路径（含端点）被重复映射，或映射端点的子树相互重叠；
- 起点仍存在于候选版本、或终点已存在于基线版本（跨版本不一致）。

## 影响分析

- **直接影响**：资产引用与目标字段 `(Schema, 版本, 路径)` 精确相等。
- **传递影响**：沿跨 Schema `$ref` 边反向传播，按字段后缀对齐；引用整个
  对象或更深子路径的资产同样命中。结果按
  `(资产标识, Schema, 版本, 稳定路径)` 去重并稳定排序。
- 影响链深度、遍历边数、影响资产数量、单次比较的分析对象字段数与变更条目数
  超过公开限制（见 `meta_catalog/limits.py`）时统一返回
  `ImpactAnalysisTooLarge`。

## 批量影响分析

`catalog.analyze_impact_batch(request)` 按一组逻辑字段筛选受影响资产，
`request` 为字典：

```python
{
    "schema": "Address",
    "version": "1.0",
    "paths": ["/street", "/zip"],   # 逻辑字段路径，按首次出现顺序去重
    "mode": "any",                  # all：命中每个字段；any：至少命中一个
    "asset_ids": [...],             # 可选；去重后限定候选范围，
                                    # 缺省覆盖全部资产，空列表不选任何资产
}
```

返回结构（普通 dict/list，可直接 JSON 序列化）：

```python
{
    "schema": "Address",
    "version": "1.0",
    "paths": ["/street", "/zip"],   # 去重后的字段（首次出现顺序）
    "mode": "any",
    "summary": {
        "candidate_assets": 4,      # 候选资产数（asset_ids 限定后）
        "selected_assets": 3,       # 入选资产数：至少命中一个字段
        "field_hits": {"/street": 3, "/zip": 2},  # 各字段命中资产数
        "matched_assets": 3,        # 最终命中资产数（应用 all/any 后）
    },
    "assets": [                     # 按 asset_id 升序
        {
            "asset_id": "svc-mail",
            "name": "邮寄服务",
            "kind": "service",
            "matched_fields": ["/street", "/zip"],  # 按输入顺序的实际命中字段
            "fields": {
                "/street": {
                    "impact_kind": "both",   # direct / transitive / both
                    "direct": {"asset_id": "svc-mail", "name": "邮寄服务",
                               "kind": "service"},   # 无直接命中时为 None
                    "transitive": [          # 传递引用证据，按稳定路径去重排序
                        {"schema": "Person", "version": "1.0",
                         "path": "/address/street", "matched_paths": ["/street"]},
                    ],
                },
            },
            "impact_kind": "both",  # 资产级命中方式（跨命中字段合并）
        },
    ],
}
```

- 命中口径与单字段 `analyze_impact` 完全一致：直接命中要求引用与字段精确
  相等，传递命中沿跨 Schema `$ref` 正向解析，根字段与父子字段沿用既有
  包含关系；证据按稳定路径去重排序。
- `any` 无命中返回空 `assets`；`all` 中任一字段无影响资产同样返回空
  `assets`。
- 错误口径：
  - 请求不是字典、`paths` 不是列表或含非字符串、`asset_ids` 不是列表或含
    非字符串 / 空字符串、`mode` 不是 `all` / `any`、路径不是合法逻辑
    JSONPointer、去重后字段集合为空，抛 `ImpactAnalysisInvalid`；
  - Schema、版本、字段或资产不存在，抛 `NotFound`；
  - 链深、引用边、单字段影响资产数或入选资产总数超过既有公开限制，抛
    `ImpactAnalysisTooLarge`；
  - 校验失败不返回部分结果。
- 该调用为只读：不注册资源、不生成报告、不写入检索索引；相同输入返回
  相同结构，返回值不与注册内容共享可变引用。

## 引用完整性审计

`catalog.check_schema_references(name=None, version=None)` 只读审计已注册
Schema 版本中的跨 Schema `$ref`（`Name@version#pointer`）；文档内 `#/`
引用不在审计范围。选择口径：

- 无参数：审计全部版本（按注册顺序）；
- 只给 `name`：按该名称的版本注册顺序审计；
- 只给 `version`：审计同版本号的全部 Schema；
- 同时给出：审计指定版本，无匹配版本返回 `NotFound`。

每条引用给出状态：

| `status` | 含义 |
|---|---|
| `resolved` | 目标版本存在，片段对应的逻辑字段可达 |
| `missing_schema` | 目标 `Name@version` 未注册（允许前向引用） |
| `invalid_pointer` | 目标版本存在，但片段不能解释为合法字段指针（容器关键字后缺字段名、下标越界/非数字、落在标量关键字上） |
| `missing_field` | 片段可解释为逻辑字段路径，但字段在目标版本不可达 |

`missing_field` 的可达性与影响分析同口径：不在目标字段表中时，沿覆盖该
路径前缀的跨 Schema `$ref` 边跳转、后缀对齐继续判定；引用环按实际引用
位点访问一次后终止。根路径为空串、数组元素为 `-`、`additionalProperties`
为 `*`。

返回普通 dict（可直接 JSON 序列化）：

```python
{
    "checked": 3,     # 受审源版本数
    "total": 3,       # 同 checked
    "resolved": 2,    # 外部引用全部可解析的源版本数（无引用也计入）
    "issues": [       # 非 resolved 项，reason 等于 status，按定位字段排序
        {"source_schema": "Person", "source_version": "1.0",
         "source_path": "/addr", "target_schema": "Addr", "target_version": "1.0",
         "target_path": "/street", "reason": "missing_field", "message": "..."},
    ],
    "references": [   # 全部引用（含 resolved），按逻辑定位六元组稳定去重与排序
        {"source_schema": "Person", "source_version": "1.0", "source_path": "/addr",
         "target_schema": "Addr", "target_version": "1.0",
         "target_path": "/street", "status": "resolved"},
    ],
}
```

无问题时 `issues` 为空且 `resolved == total`。审计为只读：不改动注册内容、
不生成报告、不写入检索索引；重复注册仍返回 `AlreadyExists`，
`compare_schemas`、`analyze_impact`、`analyze_upgrade_impact`、读取方法与
`search` 的返回均不受审计影响；相同输入返回相同结果。

## 升级影响汇总

`catalog.analyze_upgrade_impact(name, baseline_version, candidate, *, candidate_version=None, renames=None, asset_ids=None)`
在字段级变更比较之上给出按资产维度的升级影响汇总。候选文档、候选版本、
重命名映射与字段路径语义与 `compare_schemas` 完全一致；返回的 `report_id`
与相同输入调用 `compare_schemas` 得到的 `report_id` 相同。

- `asset_ids=None` 覆盖全部资产；指定列表时去重、与传入顺序无关，只覆盖
  所列资产；未知资产返回 `NotFound`。空列表表示不覆盖任何资产。
- 候选文档非法、候选/基线版本不存在、重命名映射不合法时返回
  `SchemaComparisonInvalid`；超过既有公开限制时返回 `ImpactAnalysisTooLarge`。
- 该调用为只读：不注册候选文档、不生成或改动变更报告、不写入检索索引。

返回结构（普通 dict/list，可直接 JSON 序列化）：

```python
{
    "report_id": "...",
    "schema": "Person",
    "baseline_version": "1.0",
    "candidate_version": "2.0",
    "summary": {
        "asset_total": 3,        # 入选资产总数
        "breaking_assets": 1,    # 最严重命中为 breaking 的资产数
        "compatible_assets": 1,  # 最严重命中为 compatible 的资产数
        "metadata_assets": 0,    # 最严重命中为 metadata 的资产数
        "unaffected_assets": 1,  # 无任何命中的资产数
        "changed_paths": 2,      # 入选资产命中项的 path 去重数
    },
    "assets": [
        {
            "asset_id": "svc-order",
            "name": "订单服务",
            "kind": "service",
            "status": "breaking",   # breaking > compatible > metadata > unaffected
            "changes": [
                # 保留变更报告的全部字段与既有稳定排序，新增 impact_kind
                {"path": "/age", "change_kind": "deleted",
                 "compatibility": "breaking", "impact_kind": "direct", ...},
            ],
        },
        # 无命中的资产同样列出：status=unaffected，changes=[]
    ],
}
```

- `assets` 按 `asset_id` 升序，未受影响资产也包含在内；
- 每条变更的 `impact_kind` 为 `direct`（直接命中）、`transitive`（传递命中）
  或 `both`（同一变更直接与传递同时命中）；同一资产与同一变更只保留一条，
  去重口径与字段级影响分析一致；
- 相同输入始终返回相同的字段顺序、计数、排序与结论。

## 引用迁移规划

`catalog.plan_reference_migration(name, baseline_version, candidate_version, *, renames=None)`
在同名 Schema 的两个**已注册**版本之间做只读引用迁移规划：盘点指向基线版本
的全部引用——已注册 Schema 中指向 `name@baseline_version` 的跨 Schema
`$ref`，以及资产 refs 中对该版本字段的直接引用——并逐条给出迁移建议。
`renames` 的逻辑路径与显式重命名语义和 `compare_schemas` 完全一致。

每条引用给出来源类型与定位、目标 Schema、原目标路径、建议目标版本与路径、
`status` 与唯一 `reason`：

| `status` | `reason` | 含义 |
|---|---|---|
| `ready` | `path_unchanged` | 目标未变且候选版本中存在 |
| `renamed` | `path_renamed` | 显式映射或重命名子树改变目标（深层引用保留后缀） |
| `broken` | `target_deleted` | 基线字段存在，候选版本删除且无有效映射 |
| `broken` | `baseline_target_missing` | 引用未解析到基线版本字段 |

返回普通 dict（可直接 JSON 序列化）：

```python
{
    "schema": "Address",
    "baseline_version": "1.0",
    "candidate_version": "2.0",
    "renames": [{"from": "/zip", "to": "/postal"}],
    "summary": {
        "total": 3,            # 引用总数
        "source_schemas": 1,   # 来源 Schema 版本数（按 名称@版本 去重）
        "source_assets": 2,    # 来源资产数
        "ready": 1, "renamed": 1, "broken": 1,
    },
    "references": [
        {"source_type": "schema", "source_schema": "Person",
         "source_version": "1.0", "source_path": "/home", "asset_id": None,
         "target_schema": "Address", "target_path": "/street",
         "suggested_version": "2.0", "suggested_path": "/street",
         "status": "ready", "reason": "path_unchanged"},
        {"source_type": "asset", "source_schema": None, "source_version": None,
         "source_path": None, "asset_id": "svc-zip",
         "target_schema": "Address", "target_path": "/zip",
         "suggested_version": "2.0", "suggested_path": "/postal",
         "status": "renamed", "reason": "path_renamed"},
    ],
}
```

- 不同来源的引用不合并；引用按来源定位去重并稳定排序（跨 Schema 引用按
  来源定位与目标路径在前，资产引用按资产标识与目标路径在后）；`broken`
  条目的 `suggested_version` 与 `suggested_path` 为 `None`。
- 字段存在性口径与注册/影响分析一致（沿跨 Schema `$ref` 跳转解析）；
  重命名子树中的深层引用按后缀对齐给出建议路径，映射目标在候选版本不可达
  时按 `target_deleted` 处理。
- 错误口径：名称或版本非字符串、基线与候选版本相同、`renames` 非列表、
  映射缺键或路径不是合法逻辑 JSONPointer、旧路径重复、多个旧路径映向同一
  新路径、路径不在字段集合或引用落入多个重命名祖先，抛
  `SchemaComparisonInvalid`；Schema 或版本不存在抛 `NotFound`；引用边或
  资产超过影响分析公开上限抛 `ImpactAnalysisTooLarge`。
- 该调用为只读：不注册资源、不生成报告、不写入检索索引；相同输入返回
  相同结构。

## 变更预检

`catalog.preview_changes(changes, *, search=None)` 对一批尚未提交的变更做
只读预检：不注册 Schema、不生成或改动变更报告、不写入检索索引；既有注册
内容、报告库与 `search` 结果完全不受影响，返回值为独立副本。

`changes` 为 JSON 列表，每项含 `schema`、`version`、`changeType` 及对应
内容，按列表顺序预检。`changeType` 仅限：

| `changeType` | 携带内容 | 含义 |
|---|---|---|
| `add_field` | 逻辑路径 `path`、字段定义 `definition` | 在已注册版本上新增字段（路径不得已存在） |
| `remove_field` | 逻辑路径 `path` | 删除已注册字段 |
| `change_type` | 逻辑路径 `path`、字段定义 `definition` | 替换已注册字段的结构定义 |
| `compatibility_check` | 候选 Schema 文档 `candidate` | 对整版候选做字段级兼容比较（不注册候选） |
| `unregister_schema` | 仅 `schema` + `version` 定位版本 | 注销整个版本 |

返回普通 dict（可直接 JSON 序列化）：

```python
{
    "accepted": True,
    "conflicts": [          # 批次矛盾 / 未注册字段引用；为空表示可接受
        {"code": "FIELD_NOT_FOUND", "index": 1,
         "resources": [{"index": 1, "schema": "Person",
                        "version": "1.0", "path": "/age"}]},
    ],
    "impactedItems": {
        "direct": [...],     # 直接受影响资产条目（含来源变更与兼容结论）
        "transitive": [...], # 经跨 Schema $ref 传递受影响的资产条目
    },
    "searchPreview": {
        "added": [...],      # 预计新增的索引文档（compatibility_check 的变更）
        "removed": [...],    # 预计移除（unregister_schema 的 Schema 文档）
        "replaced": [...],   # 预计替换（add/remove/change_type 对应 Schema 文档）
    },
}
```

- 空 `changes` 返回 `accepted=True` 与三个空集合。
- 集合内条目按 `schema`、`version`、`asset_id`/`report_id`、`path`、
  批次 `index` 稳定排序；每个条目带标识（`schema:Name@version` 或
  `change:<report_id>:<seq>`）、`type` 与 `reason`。
- `search` 给出时沿用 `search` 的全部条件（含 `keyword` 等），只预览在
  该条件下会命中的文档：Schema 类条目按当前索引命中过滤，新增变更条目在
  临时索引上按同一分词与打分口径模拟（不写入真实索引）。
- 错误口径：
  - 缺字段、未知 `changeType`、`changes` 不是列表、非法逻辑 JSONPointer、
    字段定义/候选文档不是合法 JSON Schema、同一路径重复出现但内容不匹配，
    抛 `SchemaComparisonInvalid`；完全相同的重复条目按一条处理；
  - 引用的 Schema、版本或资产不存在抛 `NotFoundError`；
  - 影响链深度、引用边数、分析字段数/变更条目数或影响资产数超过公开限制时
    抛 `ImpactAnalysisTooLarge`；
  - 批次内部矛盾（同一路径被不同变更类型覆盖、`add_field` 落在已注册字段
    上、`unregister_schema` 与同版本其他变更并存）或 `remove_field`、
    `change_type` 指向未注册字段时不抛错，返回 `accepted=False`，`conflicts`
    分别列 `code=CHANGE_CONFLICT` 与 `code=FIELD_NOT_FOUND`（含 `index` 与
    `resources`），此时 `impactedItems`、`searchPreview` 均为空集合。
- 兼容性判定沿用既有字段级比较；`compatibility_check` 中旧版本未定义的
  扩展字段（即使候选中为必填）不新增要求，预检中一律按 `compatible` 计入。

## 检索

`catalog.search(keyword, **filters)`，条件之间为 AND：

- `keyword`：普通关键词，按固定分词口径（拉丁连续串、中文二元组、小写化、
  AND 全命中）匹配可检索文本字段；
- 结构化过滤：`schema`、`version`、`field_path`、`change_kind`、
  `compatibility`、`asset_name`（命中直接或传递影响资产名称）、`doc_type`、
  `limit`；
- 返回条目标注 `matched_fields`（命中字段）与命中资产摘要；
- 排序为「命中字段数降序 + 确定性键升序」，与索引插入顺序无关。报告进入
  索引不改变旧关键词查询的匹配口径与排序稳定性。

### 分页检索

`catalog.search_page(keyword, **filters)` 在 `search` 的同一检索链路上提供
分页入口，检索条件与排序口径完全一致（不接受 `limit`）：

- 另接受 `page_size`（默认 50，仅 1 到 200 的普通整数）与可选 `cursor`；
  第一页不传 `cursor`，后续页只传上一页返回的 `next_cursor`；
- 每页返回 `items`（与 `search` 同条件结果逐项同构且顺序一致）、
  `total`（调用时索引命中总数）、`page_size`、`next_cursor`
  （不透明，末页为 `None`）；空结果首页 `items` 为空、`total` 为 0、
  `next_cursor` 为 `None`；
- 相同索引状态下连续翻页，命中项恰好出现一次；分页期间新增注册或报告时，
  已完成页不改变，当前页与 `total` 以调用时索引为准，新文档只有确定性键
  排在续页位置后才进入后续页；
- `cursor` 绑定完整查询条件与 `page_size`，任何一项变化都不得复用；
- `page_size` 非法（布尔、非整数、零、负数、越界）、`keyword` 或过滤值
  非字符串、出现 `limit` 等未公开参数、`cursor` 缺失内容 / 格式非法 /
  来源未知 / 与当前查询不匹配，均抛 `SearchQueryInvalid`；
- 分页入口只读注册内容与索引：不注册资源、不生成报告、不写索引。

## 不可变性与边界

- Schema 版本、版本关系与资产依赖关系注册后不可变；重复注册返回
  `AlreadyExists`；所有读取返回深拷贝。
- 内联候选文档不会被注册；比较、报告读取与检索都不会改写既有注册内容。
- 报告只通过公开接口返回，不规定落盘格式；相同输入产生相同 `report_id`、
  相同字段顺序、影响顺序与兼容结论。
- 错误码：`NotFound`、`AlreadyExists`、`SchemaComparisonInvalid`、
  `ImpactAnalysisInvalid`、`ImpactAnalysisTooLarge`、`SearchQueryInvalid`。

## 测试

```bash
python3 -m unittest discover -s tests
```
