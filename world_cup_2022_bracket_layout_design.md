# 2022卡塔尔世界杯晋级图 — 赛程图布局与 HTML 架构设计

> **任务**: T002 · 设计视觉化晋级图（Bracket）布局与 HTML5 架构
> **目标**: 1920×1080 桌面全高清（FHD）下**无横向滚动条**清晰展示
> **范围**: 四个淘汰赛轮次（16强 → 8强 → 4强 → 决赛）+ 季军赛
> **技术约束**: 单文件 HTML5，内嵌 CSS（flexbox/grid + CSS 连线 或 SVG），全部 UI 文案为中文
> **前置数据**: `world_cup_2022_knockout.json`（T001 确认的 16 场淘汰赛权威数据）

---

## 一、设计目标与 FHD 约束

| 约束 | 要求 | 设计对策 |
|------|------|----------|
| 分辨率 | 1920×1080 桌面 | 页面内容区最大宽 1720px，上下居中，两侧留白 ≥ 90px |
| 无横向滚动 | 内容总宽 ≤ 1920px | 主赛程图 `max-width: 1720px`；卡片不设固定像素总宽，列宽用 `1fr` 弹性分配 |
| 竖向高度 | 1080px 内可完整浏览 | 最高列（16强 8 张卡片）≈ 532px，加页头/名次/页脚 ≈ 820px，远小于 1080px |
| 轮次数量 | 4 个淘汰赛轮次 + 季军赛 | 4 列主赛程 + 右列下半区放季军赛 + 底部名次横条 |
| 中文文案 | 所有 UI 标签/标题/轮次名中文 | 见「六、UI 文案与轮次名称定义（中文）」 |
| 零外部依赖 | 无 CDN / 无外链字体 | 系统字体栈 + 内嵌 CSS + 内嵌 JSON 数据 |

### 像素预算（1920×1080）

| 区域 | 高度 | 宽度 |
|------|------|------|
| body 内边距 | 16px 上下 | 16px 左右（内容 1888px） |
| 页头 header（H1 + 副标题） | ≈ 96px | 100% |
| 赛程图 .bracket（8 行 × 56px + 7 间距 × 12px） | ≈ 532px | ≤ 1720px |
| 最终名次横条 | ≈ 96px | ≤ 1720px |
| 页脚 footer | ≈ 40px | 100% |
| **合计** | **≈ 780px** | **≤ 1888px** ✅ |

> ✅ 高度余量 ≈ 300px、宽度余量 ≈ 168px，任何轮次都不会触发横向滚动或纵向折叠。

---

## 二、总体布局蓝图（ASCII）

```
┌───────────────────────────────────────────────────────────────────────────────┐
│ header:  🏆 2022卡塔尔世界杯晋级图                                             │
│          淘汰赛晋级对阵 · 16强 → 8强 → 4强 → 决赛（含季军赛）· 2022-12-03 至 12-18 │
├───────────────────────────────────────────────────────────────────────────────┤
│ main.bracket (CSS Grid: 4 列 × 8 行，max-width 1720px，居中)                  │
│                                                                               │
│  列1 16强          列2 8强         列3 4强        列4 决赛/季军赛               │
│ ┌──────┐  ┌──────┐                                                          │
│ │M1 荷兰│──│ Q1  │──┐  ┌──────┐                                            │
│ │  vs美 │  └──────┘  │──│ S1   │──┐  ┌────────┐                            │
│ │M2 阿根廷│          │  └──────┘  │──│ 决赛    │ ← 冠军                     │
│ └──────┘  ┌──────┐  │             │  └────────┘                            │
│ │M3 法国│──│ Q2  │──┘             │                                         │
│ │  vs波 │  └──────┘                │                                         │
│ │M4 英格兰│                        │                                         │
│ └──────┘  ┌──────┐  ┌──────┐     │  ┌────────┐                            │
│ │M5 日本│──│ Q3  │──│ S2   │─────┘──│ 季军赛  │ ← 三四名                    │
│ │  vs克 │  └──────┘  └──────┘        └────────┘                            │
│ │M6 巴西│          ┌──────┐                                                │
│ └──────┘  ┌──────┐ │ Q4  │                                                │
│ │M7 摩洛哥│──│ Q4  │──┘                                                    │
│ │  vs西 │  └──────┘                                                        │
│ │M8 葡萄牙│                                                                 │
│ └──────┘                                                                   │
│                                                                            │
│ 最终名次横条: 🥇阿根廷(冠军) 🥈法国(亚军) 🥉克罗地亚(季军) 4.摩洛哥(第四名)     │
├───────────────────────────────────────────────────────────────────────────────┤
│ footer: 数据来源：T001 确认的淘汰赛数据 · 静态内嵌 · 无外部依赖                 │
└───────────────────────────────────────────────────────────────────────────────┘
```

**布局要点**：
- **4 列主赛程**（16强 / 8强 / 4强 / 决赛）+ **右列下半区季军赛**，与 FIFA 官方晋级图一致；
- 8 行等高的 Grid 行轨道（56px/行），跨行+垂直居中实现「上一轮两场 → 下一轮一场」的经典几何对齐；
- 决赛卡纵向居中于全图，季军赛卡居中于下半区（两条半决赛负者路径汇聚处）；
- 名次横条位于赛程图下方，不占用赛程列宽。

---

## 三、HTML5 结构设计（元素树）

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="description" content="2022卡塔尔世界杯晋级图：16强至决赛与季军赛的视觉化晋级对阵">
  <title>2022卡塔尔世界杯晋级图</title>
  <style> /* 内嵌 CSS —— 见第四节 */ </style>
</head>
<body>
  <header class="page-header">
    <h1>2022卡塔尔世界杯晋级图</h1>
    <p class="subtitle">淘汰赛晋级对阵 · 16强 → 8强 → 4强 → 决赛（含季军赛）· 2022-12-03 至 2022-12-18</p>
  </header>

  <main>
    <!-- ① 静态内嵌比赛数据（与现有 world-cup-2022-bracket.html 同构） -->
    <script type="application/json" id="match-data">{ ...T001 数据... }</script>

    <!-- ② 赛程图容器：相对定位，承载 Grid 卡片与 SVG 连线层 -->
    <section class="bracket" aria-label="2022卡塔尔世界杯淘汰赛晋级图">

      <!-- SVG 连线层（垫底，绝对定位，pointer-events:none） -->
      <svg class="connectors" viewBox="0 0 1720 532" preserveAspectRatio="none" aria-hidden="true">
        <!-- 16强→8强 8条、8强→4强 4条、4强→决赛 2条、4强负者→季军赛 2条 -->
      </svg>

      <!-- 轮次列标题（4 个，绝对定位在列顶或作为 grid 首行） -->
      <h2 class="round-title round-title-1">16强 · 八分之一决赛</h2>
      <h2 class="round-title round-title-2">8强 · 四分之一决赛</h2>
      <h2 class="round-title round-title-3">4强 · 半决赛</h2>
      <h2 class="round-title round-title-4">决赛</h2>

      <!-- ③ 比赛卡片（每张卡片一个 <article class="match">） -->
      <article class="match m1"  data-round="round_of_16" data-match="1"  data-winner="home">…</article>
      <article class="match m2"  data-round="round_of_16" data-match="2"  data-winner="away">…</article>
      … 共 16 张卡片 …
      <article class="match m16" data-round="final"        data-match="16" data-winner="home">…</article>

      <!-- ④ 季军赛卡片（右列下半区） -->
      <article class="match third-place" data-round="third_place_match" data-match="15">…</article>
    </section>

    <!-- ⑤ 最终名次横条 -->
    <section class="standings" aria-labelledby="standings-title">
      <h2 id="standings-title" class="visually-hidden">最终名次</h2>
      <ol class="standings-list"> …4 项名次… </ol>
    </section>
  </main>

  <footer class="page-footer">
    <p>2022卡塔尔世界杯晋级图 · 数据来源：T001 确认的淘汰赛比赛数据（静态内嵌，无外部依赖）</p>
  </footer>
</body>
</html>
```

### 结构决策

| 决策点 | 选择 | 理由 |
|--------|------|------|
| 卡片语义元素 | `<article class="match">` | 每场比赛是独立内容单元，利于 aria 标注与后续 JS 高亮 |
| 数据载体 | `<script type="application/json" id="match-data">` | 复用 T001 数据，保持"单文件静态内嵌"约定 |
| 渲染方式 | **静态手写 DOM 卡片**（零 JS） | 历史数据已固定，无需运行时渲染，最大兼容性；JS 仅可选用于连线坐标计算 |
| 轮次标题 | 4 个 `<h2 class="round-title">` | 随 Grid 定位到各列顶部，`aria-labelledby` 关联 |
| 最终名次 | `<ol class="standings-list">` | 有序列表天然表达名次语义 |
| 语言 | `<html lang="zh-CN">` | 全中文界面，利于屏幕阅读器发音 |

---

## 四、内嵌 CSS 方案（Grid + CSS 连线 / SVG）

### 4.1 设计变量（CSS 自定义属性）

```css
:root {
  --bg: #f4f6f8;
  --card-bg: #ffffff;
  --border: #d0d7de;
  --line: #9aa4af;          /* 连线颜色 */
  --text: #1f2328;
  --muted: #656d76;
  --accent: #0b6e4f;        /* 晋级/胜者强调色 */
  --win-bg: #e8f5ef;        /* 胜者行底色 */
  --card-w: 240px;          /* 卡片宽度 */
  --card-h: 56px;           /* 卡片高度 = 1 个 Grid 行轨道 */
  --row-gap: 12px;          /* Grid 行间距 */
  --col-gap: 32px;          /* Grid 列间距（连线走廊） */
}
```

### 4.2 赛程图容器（CSS Grid）

```css
.bracket {
  position: relative;                 /* 供 SVG 连线层绝对定位 */
  display: grid;
  grid-template-columns: repeat(4, minmax(240px, 1fr));  /* 4 列弹性，不超宽 */
  grid-template-rows: repeat(8, var(--card-h));          /* 8 行等高轨道 */
  column-gap: var(--col-gap);
  row-gap: var(--row-gap);
  max-width: 1720px;
  margin: 0 auto;
  padding: 0 8px;
}

/* 轮次标题：绝对定位于每列顶部（避开 Grid 行轨道占用） */
.round-title { position: absolute; top: -34px; font-size: 16px;
               font-weight: 700; color: var(--accent); letter-spacing: 1px; }
.round-title-1 { left: 0; }
.round-title-2 { left: calc(25% + var(--col-gap)/2); }
.round-title-3 { left: calc(50% + var(--col-gap)); }
.round-title-4 { left: calc(75% + var(--col-gap)*1.5); }
```

### 4.3 比赛卡片定位（跨行 + 垂直居中）

```css
.match {
  width: var(--card-w);
  min-height: var(--card-h);
  background: var(--card-bg);
  border: 1px solid var(--border);
  border-radius: 6px;
  z-index: 1;                          /* 盖在 SVG 连线上方 */
}
/* 16强：8 张卡，依 Grid 自动流入第 1 列的第 1~8 行 */
.m1, .m2, .m3, .m4, .m5, .m6, .m7, .m8 { grid-column: 1; }
/* 8强：4 张卡，各占 2 行，垂直居中于对应的两个 16强卡之间 */
.q1 { grid-column: 2; grid-row: 1 / span 2; align-self: center; }
.q2 { grid-column: 2; grid-row: 3 / span 2; align-self: center; }
.q3 { grid-column: 2; grid-row: 5 / span 2; align-self: center; }
.q4 { grid-column: 2; grid-row: 7 / span 2; align-self: center; }
/* 4强：2 张卡，各占 4 行，垂直居中于上半区/下半区 */
.s1 { grid-column: 3; grid-row: 1 / span 4; align-self: center; }
.s2 { grid-column: 3; grid-row: 5 / span 4; align-self: center; }
/* 决赛：整列 8 行居中 */
.final-card    { grid-column: 4; grid-row: 1 / span 8; align-self: center; }
/* 季军赛：右列下半区 4 行居中（半决赛负者路径汇聚处） */
.third-place   { grid-column: 4; grid-row: 5 / span 4; align-self: center; }
```

> 该几何布局自动保证：16强第 1、2 场的中线与 8强第 1 场的中线在同一垂直高度，连线呈标准「双线汇入」形态。

### 4.4 连线策略（主方案：内嵌 SVG；备选：纯 CSS 连线）

#### 主方案 — SVG 连线层（推荐）

```css
.connectors {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  z-index: 0;                    /* 垫在卡片之下 */
  pointer-events: none;          /* 不拦截交互 */
}
```

```html
<svg class="connectors" viewBox="0 0 1720 532" preserveAspectRatio="none" aria-hidden="true">
  <!-- 16强 M1→8强 Q1（示例）：右出-竖折-横入 -->
  <path class="c" d="M 240 28 H 264 V 56 H 288"
        fill="none" stroke="#9aa4af" stroke-width="2"
        vector-effect="non-scaling-stroke"/>
  <path class="c" d="M 240 84 H 264 V 56 H 288" .../>
  <!-- 共 8 条 16强→8强、4 条 8强→4强、2 条 4强→决赛、2 条 4强负者→季军赛 -->
</svg>
```

- **坐标可由几何公式静态推导**（无需 JS）：列中心 x = `col_idx × (25% 宽 + gap)`，卡片中线 y = `(row_start-1)×(56+12) + 28`；因 Grid 轨道是固定高度（56px+12px），设计期即可算出全部 16 条路径；
- `vector-effect="non-scaling-stroke"` 保证 `preserveAspectRatio="none"` 拉伸时线宽仍为 2px；
- 晋级高亮：胜者连线加 `stroke="#0b6e4f"`，负者连线用浅灰，直观表达晋级路径。

#### 备选方案 — 纯 CSS 连线（无 SVG）

```css
.match::after {
  content: '';
  position: absolute;
  right: calc(-1 * var(--col-gap) / 2);
  top: 50%;
  width: calc(var(--col-gap) / 2);
  height: var(--elbow, 0px);        /* 每条路径的竖向偏移，按对设置 */
  border-top: 2px solid var(--line);
  border-right: 2px solid var(--line);
}
/* 下一轮卡片 ::before 向左补一条横线，与 ::after 的竖线汇合 */
```

- 优点：零 SVG、纯 CSS；缺点：每条路径需单独设置 `--elbow` 偏移量，代码冗余、维护性差；
- **结论：默认采用 SVG 方案**，CSS 连线仅作为禁用 SVG 场景的降级方案（文档保留）。

### 4.5 卡片内部布局（flexbox）

```html
<article class="match m1">
  <div class="match-row winner">   <!-- 胜者行：底色高亮 -->
    <span class="team">荷兰</span>
    <span class="score">3</span>
  </div>
  <div class="match-row">
    <span class="team">美国</span>
    <span class="score">1</span>
  </div>
  <span class="match-date">12-03</span>
  <span class="penalty" hidden>点球 4-2</span>
</article>
```

```css
.match { display: flex; flex-direction: column; justify-content: center; }
.match-row {
  display: flex;
  justify-content: space-between;   /* 队名靠左、比分靠右 */
  align-items: center;
  padding: 2px 8px;
  font-size: 14px;
  line-height: 22px;
}
.match-row.winner { background: var(--win-bg); color: var(--accent); font-weight: 700; }
.match-date { position: absolute; top: -18px; right: 2px; font-size: 11px; color: var(--muted); }
.penalty { font-size: 11px; color: var(--muted); }
```

---

## 五、FHD 无横向滚动保证清单

| 检查项 | 数值 | 状态 |
|--------|------|------|
| 赛程图 max-width | 1720px | ≤ 1888px（1920−32 内边距） ✅ |
| 4 列弹性最小宽 | 4 × 240px + 3 × 32px gap = 1056px | 不超宽，多余空间平均分配 ✅ |
| 卡片宽度 | 240px 固定，不随列宽放大 | 卡片内容不换行 ✅ |
| body 溢出控制 | `overflow-x: hidden`（保险）+ `max-width` 双重保障 | ✅ |
| 最高列高度 | 8 × 56 + 7 × 12 = 532px | ≤ 1080 可视区 ✅ |

---

## 六、UI 文案与轮次名称定义（中文）

### 6.1 页面级文案

| 键 | 中文文案 | 备注 |
|----|----------|------|
| 页面标题 `<title>` | 2022卡塔尔世界杯晋级图 | |
| 主标题 H1 | 2022卡塔尔世界杯晋级图 | |
| 副标题 | 淘汰赛晋级对阵 · 16强 → 8强 → 4强 → 决赛（含季军赛）· 2022-12-03 至 2022-12-18 | |
| 页脚 | 2022卡塔尔世界杯晋级图 · 数据来源：T001 确认的淘汰赛比赛数据（静态内嵌，无外部依赖） | |

### 6.2 轮次名称（round_name / 列标题）

| 数据键（JSON） | 轮次中文名（正式） | 列标题（UI 显示） | 英文对照 |
|----------------|--------------------|-------------------|----------|
| `round_of_16` | 16强 / 八分之一决赛 | 16强 · 八分之一决赛 | Round of 16 |
| `quarterfinals` | 8强 / 四分之一决赛 | 8强 · 四分之一决赛 | Quarter-finals |
| `semifinals` | 4强 / 半决赛 | 4强 · 半决赛 | Semi-finals |
| `final` | 决赛 | 决赛 | Final |
| `third_place_match` | 季军赛 | 季军赛（三四名决赛） | Third Place Match |

### 6.3 卡片 / 结果类文案

| 键 | 中文文案 | 说明 |
|----|----------|------|
| 胜者行标识 | 晋级 | 胜者行底色高亮 + 加粗（aria-label 用） |
| 比分 | 比分 | 卡片内 主队 比分 客队 |
| 点球 | 点球 | 例：点球 4-2（仅点球决胜场次显示） |
| 日期 | 日期 | 例：12-03（月-日，省略年份） |
| 冠军 | 冠军 | 决赛胜者 |
| 亚军 | 亚军 | 决赛负者 |
| 季军 | 季军 | 季军赛胜者 |
| 第四名 | 第四名 | 季军赛负者 |

### 6.4 名次横条文案

| 键 | 中文文案 |
|----|----------|
| 名次区标题 | 最终名次 |
| 名次条目 | 🥇 阿根廷（冠军）· 🥈 法国（亚军）· 🥉 克罗地亚（季军）· 4. 摩洛哥（第四名） |

### 6.5 无障碍与辅助文案

| 键 | 中文文案 |
|----|----------|
| 赛程图 aria-label | 2022卡塔尔世界杯淘汰赛晋级图 |
| 卡片 aria-label | 例：十六分之一决赛 第1场：荷兰 3-1 美国，晋级方荷兰 |
| 连线层 | `aria-hidden="true"`（纯装饰，不朗读） |

---

## 七、数据绑定（JSON → DOM 映射）

| JSON 字段 | 目标 DOM | 说明 |
|-----------|----------|------|
| `rounds.round_of_16.matches[0..7]` | `.m1`~`.m8` 卡片 | 主队=首行、客队=次行；`winner` 决定哪行加 `.winner` |
| `rounds.quarterfinals.matches[0..3]` | `.q1`~`.q4` 卡片 | 同上 |
| `rounds.semifinals.matches[0..1]` | `.s1`、`.s2` 卡片 | 同上 |
| `rounds.final.matches[0]` | `.final-card` | `note` 字段（90分钟/加时/点球）渲染为卡片脚注 |
| `rounds.third_place_match.matches[0]` | `.third-place` | 同上 |
| `final_standings[0..3]` | `.standings-list` 的 `<li>` | rank/team/result |
| `penalty_score` 非空 | 卡片 `.penalty` 元素 | 例：点球 4-2 |
| 连线（晋级路径） | SVG `<path>` | `advancement_paths` 可校验每条连线两端 |

> 实现策略：因数据固定，**卡片与 SVG 路径均由构建期静态生成**写入 HTML（零运行时 JS）；后续如需扩展，可用少量 JS 从 `#match-data` 渲染并测量 DOM 计算 SVG 坐标（已在架构中预留该能力）。

---

## 八、可访问性 / 响应式 / 降级

1. **可访问性**：全中文 `lang="zh-CN"`；卡片为真实文本（非图片）；SVG 连线 `aria-hidden`；名次用 `<ol>` 表达顺序；胜者行加 `aria-label="晋级"`。
2. **< 1920px 降级**：`@media (max-width: 1720px)` 时列 `minmax(200px, 1fr)`、卡片宽 200px、字号 13px，仍不横向滚动；`@media (max-width: 900px)` 时退化为纵向堆叠的轮次分区（复用现有 `world-cup-2022-bracket.html` 的表格结构思路）。
3. **打印**：`@media print` 隐藏 header/footer，SVG 连线保留（`vector-effect` 保证线宽）。
4. **无 JS 环境**：页面为纯静态 HTML+CSS，卡片与连线均无需 JS 即可完整显示。

---

## 九、与现状的差异（现有 `world-cup-2022-bracket.html` → 新架构）

| 维度 | 现有页面 | 新设计 |
|------|----------|--------|
| 布局 | 表格分节，max-width 960px | 视觉化晋级图，Grid 4 列，max-width 1720px |
| 轮次呈现 | 6 个 `<section>` 表格 | 4 列赛程 + 右列季军赛 + 名次横条 |
| 连线 | 无 | SVG 折线（或 CSS 伪元素降级） |
| 中文文案 | 已有轮次名 | 统一为完整中文标签体系（见第六节） |
| FHD | 表格居中浪费横向空间 | 全宽利用 1720px，无横向滚动 |

---

## 十、验收对应（Acceptance Mapping）

| 验收要求 | 设计落实 |
|----------|----------|
| 无横向滚动（1920×1080） | 赛程图 1720px ≤ 1888px 可用宽；Grid `1fr` 弹性；`overflow-x` 保险（第五节） |
| 展示四个淘汰赛轮次 + 季军赛 | 第 1~3 列 = 16强/8强/4强；第 4 列 = 决赛（上）+ 季军赛（下）（第二节） |
| flexbox/grid 布局 | 赛程图 Grid、卡片内 flexbox（第四节） |
| CSS 连线 或 SVG | 主方案 SVG 连线层；备选 CSS `::after` 折线（4.4） |
| 全部 UI 标签中文 | 第六节完整中文文案表 |

---

*设计文档结束 — 可作为 T003「实现视觉化晋级图」的编码规格书。*
