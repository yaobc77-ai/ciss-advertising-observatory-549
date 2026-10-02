/* Rebuild the 0.2.2 research preview with the bundled Artifact Tool.
 * Run with the bundled node.exe. Existing final files are never overwritten.
 * Override ARTIFACT_NODE_MODULES / PRESENTATIONS_SKILL_DIR / ARTIFACT_PYTHON
 * when the bundled runtime lives elsewhere. No database or API is used.
 */
async function main() {
  const fs = await import('node:fs/promises');
  const path = await import('node:path');
  const {pathToFileURL} = await import('node:url');
  const workspaceDir = path.resolve(__dirname, '..');
  const home = process.env.USERPROFILE;
  const runtime = path.join(home, '.cache/codex-runtimes/codex-primary-runtime/dependencies');
  const modules = process.env.ARTIFACT_NODE_MODULES || path.join(runtime, 'node/node_modules');
  process.env.RUNTIME_NODE_MODULES = modules;
  const skill = process.env.PRESENTATIONS_SKILL_DIR || path.join(home, '.codex/plugins/cache/openai-primary-runtime/presentations/26.909.12148/skills/presentations');
  const python = process.env.ARTIFACT_PYTHON || path.join(runtime, 'python/python.exe');
  const {Presentation, PresentationFile, FileBlob} = await import(pathToFileURL(path.join(modules, '@oai/artifact-tool/dist/artifact_tool.mjs')).href);
  const {resolvePresentationFont, applyPresentationChartFont, finalizePresentation} = await import(pathToFileURL(path.join(skill, 'container_tools/artifact_tool_utils.mjs')).href);
  const tmp = path.join(workspaceDir, '.runtime/preview-v0_2_2');
  const final = path.resolve(workspaceDir, process.env.PREVIEW_OUTPUT || 'deliverables/research_preview_v0_2_2.pptx');
  await fs.mkdir(tmp, {recursive:true});
  await fs.mkdir(path.dirname(final), {recursive:true});
  const runPath = path.join(workspaceDir, 'outputs/development_claim_contract_20260916.json');
  const run = JSON.parse(await fs.readFile(runPath, 'utf8'));
  if (run.run_id !== 'd26eb540a6114cfe9672a755c43f39a7' || run.data_version !== 'd85a98002e4493f0376c260ad82253ee') throw Error('The declared preview requires its saved 0.2.2 development run.');
  const stats = run.summary.native;
  const dev7 = run.cases.find(c => c.id === 'dev-07');
  const quote = dev7.citations[0];
  const evidence = dev7.evidence.find(e => e.evidence_id === quote.evidence_id);
  const quoteIndex = evidence.text.indexOf(quote.quote);
  if (quoteIndex < 0) throw Error('Quoted passage is not present in the saved evidence.');
  const offsetStart = evidence.start + quoteIndex;
  const offsetEnd = offsetStart + quote.quote.length;
  const font = resolvePresentationFont({fontFamily:'Microsoft YaHei'});
  const C={bg:'#F5F2E9',ink:'#183C3A',teal:'#176B63',muted:'#596B66',light:'#D9E5DF',line:'#C6D0C8',amber:'#93622C',white:'#FFFFFF'};
  const p = Presentation.create({slideSize:{width:1280,height:720}});
  function text(s,value,x,y,w,h,size=26,color=C.ink,bold=false) {
    const sh=s.shapes.add({geometry:'textbox',position:{left:x,top:y,width:w,height:h},fill:'none',line:{fill:'none',width:0}});
    sh.text=value; sh.text.style={typeface:font,fontSize:size,color,bold,autoFit:'none'};
    return sh;
  }
  function line(s,x,y,w){s.shapes.add({geometry:'line',position:{left:x,top:y,width:w,height:0},fill:'none',line:{fill:C.line,width:1}});}
  function slide(title,kicker,notes){
    const s=p.slides.add();s.background.fill=C.bg;
    text(s,kicker,64,32,1140,32,18,C.teal,true);
    text(s,title,64,77,1152,69,44,C.ink,true);
    line(s,64,653,1152);
    text(s,'CISS Observatory   研究预览 0.2.2   2026-09-16',64,668,1060,24,16,C.muted);
    text(s,String(p.slides.items.length).padStart(2,'0'),1160,663,54,28,18,C.muted);
    s.speakerNotes.textFrame.setText(notes);
    return s;
  }
  function table(s,values,widths,x=64,y=172,h=410,fontSize=23){
    const t=s.tables.add({rows:values.length,columns:values[0].length,left:x,top:y,width:widths.reduce((a,b)=>a+b,0),height:h,columnWidths:widths,values});
    t.styleOptions={headerRow:true,bandedRows:false};
    t.borders.assign({style:'solid',fill:C.line,width:0.7});
    for(let r=0;r<values.length;r++) for(let c=0;c<values[0].length;c++) {
      const cell=t.getCell(r,c);cell.fill=r===0?C.teal:C.bg;
      cell.text.style={typeface:font,fontSize,color:r===0?C.white:C.ink,bold:r===0,autoFit:'none'};
    }
    return t;
  }
  // 1: minimal title. The established ivory / teal project identity is retained.
  {
    const s=p.slides.add();s.background.fill=C.bg;
    text(s,'FA26  DS 549  /  CISS',64,55,1110,35,24,C.teal,true);
    text(s,'化石燃料与动物农业\n广告观测站',64,174,1150,175,64,C.ink,true);
    text(s,'研究预览 0.2.2',68,390,1090,57,40,C.teal,true);
    text(s,'Native corpus connected',68,461,1080,45,30,C.ink);
    text(s,'本地原生语料已连接，最终验收仍待完成。',68,540,1080,42,27,C.muted);
    line(s,64,653,1152);text(s,'2026-09-16',64,670,1110,25,18,C.muted);
    s.speakerNotes.textFrame.setText('本演示是 FA26 项目的研究预览，展示当前已运行的原生数据看板与带证据问答。双数据集交付目标不变。真实社交语料、指定 GitHub、固定域名和人工验收尚未完成。依据：FA26_DELIVERY_PLAN.zh-CN.md；docs/handoff.md；reports/claim_contract_v0_2_2.md。');
  }
  // 2: editable module-status matrix.
  {
    const s=slide('八个模块围绕双数据集与带证据问答','项目交付目标','来源：FA26_DELIVERY_PLAN.zh-CN.md 的 M1–M8；docs/handoff.md；reports/claim_contract_v0_2_2.md。该矩阵描述本地实施与剩余依赖，不代表各模块全部通过验收。CLAIMS 只使用保存的历史标签，本期没有重建分类后端或训练模型。');
    table(s,[['模块','当前已有','下一项交付依赖'],['M1  数据导入','原生数据、版本与来源绑定','真实社交导出与字段核对'],['M2  正文与来源','质量标记、原文区间、引用定位','截断候选的身份与页面核对'],['M3  原生看板','六字段、图表、筛选与导出','持续对账和用户验收'],['M4  社交视图','独立状态与适配接口','真实数据尚未连接'],['M5  检索问答','关键词、向量、SQL 与带证据回答','人工判断语义充分性'],['M6  评价','开发诊断与 20＋20 题草案','冻结验收题并独立人审'],['M7  部署与费用','Windows 本地运行、预算与备份','固定域名与外部访问验收'],['M8  交接','源码、说明、测试、研究预览','指定 GitHub 与最终双库演示']],[240,456,456],64,164,440,22);
    text(s,'CLAIMS：保留历史自动标签及其来源，后端集成列为未来工作。',64,612,1152,32,22,C.muted);
  }
  // 3: real counts, represented as an editable chart with a workbook snapshot.
  {
    const s=slide('同一批语料使用不同的资格口径','M1–M2  数据与质量','来源：docs/handoff.md；reports/data_revision_v0_2.md；outputs/native_import_v0_2_published_repeat_20260916.json；reports/truncation_recovery_preflight.md。数据版本 d85a98002e4493f0376c260ad82253ee。275 收录记录包括元数据-only 项；263 可计数；226 合格检索正文；554 检索 chunks。94 是疑似截断的质量警告，既不是 94 次检索失败，也不表示剩余内容均不可服务。现有结构化来源确认直接恢复全文 0 条，3 个 PDF 续文候选尚未核实身份和视觉边界。');
    const chart=s.charts.add('bar',{position:{left:70,top:172,width:820,height:336},categories:['收录记录','可计数记录','可检索正文'],series:[{name:'记录数',values:[275,263,226],fill:C.teal}],barOptions:{direction:'column',grouping:'clustered',gapWidth:110},hasLegend:false,dataLabels:{showValue:true,position:'outEnd',textStyle:{typeface:font,fontSize:28,fill:C.ink,bold:true}},xAxis:{textStyle:{typeface:font,fontSize:24,fill:C.ink}},yAxis:{min:0,max:300,majorUnit:100,numberFormatCode:'0',textStyle:{typeface:font,fontSize:18,fill:C.muted}},chartFill:C.bg,plotAreaFill:C.bg,chartLine:{fill:'none',width:0},plotAreaLine:{fill:'none',width:0}});
    applyPresentationChartFont(chart,{fontFamily:font});
    text(s,'554',945,199,260,95,70,C.teal,true);text(s,'检索文本块',947,301,260,37,27,C.ink);
    text(s,'94',945,393,260,76,60,C.amber,true);text(s,'疑似截断警告',947,477,280,38,26,C.ink);
    text(s,'保留原文和质量标记，按可用区间检索。',64,555,1152,37,27,C.ink,true);
    text(s,'结构化来源尚未确认恢复全文；3 个 PDF 续文候选仍需核对。',64,607,1152,31,22,C.muted);
  }
  // 4: six fields and dashboard behavior, all editable.
  {
    const s=slide('筛选、图表和 CSV 共享同一记录集合','M3  原生广告 Dashboard','来源：docs/user_guide.md；docs/handoff.md。公众界面使用英文。六字段为 URL、publisher、title、date、sponsor、keyword。公司和媒体的 count/percent、timeline、sponsor-publisher 关系均按筛选后的可计数记录计算。曾通过浏览器验证 exxonmobil 15 个不同 record_id，与导出 CSV 的 15 条一致。此历史功能证据不等于本轮重跑浏览器。来源链接关闭时页面、证据和导出 URL 一并隐藏。unknown 与 historical labels 保留说明。');
    table(s,[['六字段','研究者可做的操作'],['URL、publisher、title','查看文章与媒体，按配置打开来源'],['date、sponsor、keyword','筛选日期、赞助方和词项，保留 Unknown'],['数量、占比与时间','对账公司／媒体统计和时间分布'],['赞助关系与导出','检查公司与媒体连接，导出当前集合']],[430,722],64,173,306,25);
    text(s,'ExxonMobil  15 条',64,518,640,55,39,C.teal,true);
    text(s,'保存的浏览器核对：筛选 15 个记录 ID，CSV 导出同为 15 条。',64,586,1150,37,24,C.muted);
  }
  // 5: directly selected evidence from the actual run, never recast as verified fact.
  {
    const s=slide('回答同时保留广告归因、数量对象和规划状态','M5  原文定位与回答约束',`来源：outputs/development_claim_contract_20260916.json；reports/claim_contract_v0_2_2.md；reports/assisted_semantic_review_v0_2_2.md。run ${run.run_id}，dev-07。下方英文是广告原句，不能证明现实中设施已建成或产量已实现。record_id=${evidence.record_id}；version_id=${evidence.version_id}；evidence_id=${evidence.evidence_id}；字符区间 [${offsetStart},${offsetEnd})；paragraph_ids=${evidence.paragraph_ids.join(',')}；source=${evidence.url}。程序从被选 passage_id 还原原文和坐标，模型不手工计算偏移。语言沿用问题语言，并用保守本地检查拦截明显错语，短文本仍可能不确定。`);
    text(s,'实际广告引文',64,177,1100,33,23,C.teal,true);
    text(s,quote.quote,64,228,1152,154,29,C.ink);
    line(s,64,404,1152);
    text(s,'广告称：设施建成后，蓝氢日产量可能达到最多 10 亿立方英尺。',64,431,1152,87,32,C.teal,true);
    text(s,'归因：广告称     对象：蓝氢     单位：立方英尺／日     状态：可能、建成后',64,536,1152,56,23,C.muted);
    text(s,`原文字符区间 [${offsetStart}, ${offsetEnd})  /  记录 0d2b4bc2…  /  版本 7edbf661…`,64,606,1152,29,20,C.muted);
  }
  // 6: implemented architecture and spending boundary.
  {
    const s=slide('本机服务与付费模型承担不同职责','M5–M7  运行与费用','来源：docs/architecture.md；docs/operations.md；docs/handoff.md；FA26_DELIVERY_PLAN.zh-CN.md；outputs/development_claim_contract_20260916.json。已有 Windows 隔离 PostgreSQL 和 pgvector，本机 127.0.0.1:55432；Dash 127.0.0.1:8050。本机无需再次下载 PostgreSQL。Dash/Plotly/AG Grid 展示，pandas/Pandera 导入校验，PostgreSQL/pgvector 版本、统计、检索，pySBD 切句与校验，Pydantic 和 OpenAI SDK 生成结构化输出，Lingua 保守语言检查。预算为应用月度 $100 配置目标，调用前预留，成功或失败后按实际用量结算。Luna 与 embedding 均可能计费，SQL 计数和词项搜索不调用模型。');
    table(s,[['组件','已实现职责'],['Python Dash、Plotly、AG Grid','六字段、筛选、图表、导出和问答入口'],['PostgreSQL、pgvector','原文版本、SQL 计数、词项与向量检索'],['OpenAI SDK、Pydantic、pySBD','结构化回答、原句目录与字符切片校验'],['Luna、embedding、Lingua','付费生成／向量，本地保守语言检查']],[480,672],64,174,326,24);
    text(s,'免费搜索与 SQL 计数',64,539,585,40,30,C.teal,true);
    text(s,'Paid answer 单独触发',668,539,548,40,30,C.teal,true);
    text(s,'应用预算 $100／月，调用前预留、调用后结算。模型不可用时保留检索。',64,603,1152,32,23,C.muted);
  }
  // 7: exact denominators and honest test scope.
  {
    const s=slide('最新开发运行可追溯，人工验收仍待完成','M6  验证范围',`来源：outputs/development_claim_contract_20260916.json；reports/claim_contract_v0_2_2.md；reports/assisted_semantic_review_v0_2_2.md；reports/citation_review_v0_2_2.csv。run ${run.run_id}；数据 ${run.data_version}；draft_not_frozen。13 ready：8 answered、2 count_only、3 insufficient_evidence。4 social + 3 cross pending 不计通过。${stats.language_check_statuses.match} match 是本地语言启发式。当前 16 行人工字段为空。AI 复核指出本次 dev-01 归因和 dev-07 蓝氢对象已补齐；dev-03 经济挑战的完整性和 dev-05 BIOMEM 额外背景仍需要判断。开发题已用于迭代，不能当盲测或稳健性结论。0.2.2 相关 52 项测试通过，0.2.1 全套 164 项仅保留历史证据，本轮未重跑完整套件。`);
    const fmt=k=>`${stats[k].numerator} / ${stats[k].denominator}`;
    table(s,[['开发检查','保存结果'],['命中记录 / 必需片段',`${fmt('hit_at_5')}  /  ${fmt('support_passage_coverage')}`],['证据定位 / 引文定位',`${fmt('evidence_locator_valid')}  /  ${fmt('citation_locator_valid')}`],['SQL 计数 / 无证据拒答',`${fmt('count_exact')}  /  ${fmt('abstention_on_no_evidence')}`],['回答语言启发式',`${stats.language_check_statuses.match} match`],['人工语义支持率','未测量']],[448,312],64,174,338,24);
    text(s,'8 回答\n2 计数\n3 拒答',891,186,320,177,39,C.teal,true);
    text(s,`本轮费用\n$${stats.settled_cost_usd.toFixed(8)}`,891,409,320,84,25,C.ink);
    text(s,'0.2.2：相关 52 项测试通过。0.2.1：历史全套 164 项通过。',64,556,1152,40,26,C.ink,true);
    text(s,'开发草案已用于迭代；社交／跨库 7 题待数据，16 条引用待人工裁定。',64,607,1152,32,23,C.muted);
  }
  // 8: concrete handoff and remaining deliverables.
  {
    const s=slide('交接已有实现，继续完成双数据集交付','M4、M6–M8  交付边界','来源：docs/handoff.md；docs/operations.md；docs/user_guide.md；docs/evaluation_protocol.md；FA26_DELIVERY_PLAN.zh-CN.md；reports/claim_contract_v0_2_2.md。接手顺序：README / handoff，Start-Observatory.ps1，浏览器原生筛选和免费搜索，查看已保存 RAG 输出及 citation_review CSV，补真实社交，再完成域名和 GitHub，冻结题库进行独立人工验收，最终双数据集演示。原始资料需要单独提供，密钥和数据库运行时不进源码包。当前 deck 不是 final_presentation 或最终验收证书。');
    text(s,'接手入口',64,186,535,40,31,C.teal,true);
    text(s,'README 与 handoff\n运行说明、用户指南、架构与数据字典\n锁定依赖、脚本、测试与保存的开发证据\n本版 PPTX、中文演示脚本和人工复核表',64,252,535,270,26,C.ink);
    text(s,'仍需完成',679,186,537,40,31,C.amber,true);
    text(s,'真实社交导出与跨库验证\n指定 GitHub 仓库交付\nCloudflare 固定域名与外部访问\n独立人工验收及最终双数据集演示',679,252,537,270,26,C.ink);
    text(s,'单人主导、AI 辅助。按模块收束交付，保留每项证据与未决边界。',64,585,1152,41,28,C.ink,true);
  }
  const candidate=path.join(tmp,'candidate.pptx');
  await (await PresentationFile.exportPptx(p)).save(candidate);
  const result=await finalizePresentation({workspaceDir,candidatePath:candidate,finalPath:final,pythonExecutable:python,integrityValidatorPath:path.join(skill,'container_tools/inspect_presentation_package_integrity.py'),layoutValidatorPath:path.join(skill,'container_tools/inspect_presentation_layout_geometry.py'),explicitTotalSlideCount:8,requiredNativeTableOwnerSlides:[2,4,6,7],requiredNativeChartOwnerSlides:[3],materializeLiteralChartWorkbooks:true,layoutArgs:['--expected-slide-size-emu','12192000,6858000','--validate-bullet-geometry','--validate-heading-fit',...[2,4,6,7].flatMap(n=>['--require-native-table-slide',String(n)])],fontPolicy:{basis:'design',families:[font]},verifyArtifactToolImport:true,receiptPath:path.join(tmp,'validation.json')});
  console.log(JSON.stringify(result));
  const restored=await PresentationFile.importPptx(await FileBlob.load(final));
  for(let i=0;i<restored.slides.items.length;i++){
    const slide=restored.slides.items[i];
    const blob=await restored.export({slide,format:'png',scale:1});
    await fs.writeFile(path.join(tmp,`slide-${i+1}.png`),new Uint8Array(await blob.arrayBuffer()));
    console.log(`Rendered ${i+1}/8`);
  }
}
main().catch(error=>{console.error(error);process.exitCode=1;});
