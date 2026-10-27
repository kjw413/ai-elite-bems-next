import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { Workbook, SpreadsheetFile } from '@oai/artifact-tool';

const baseDir = path.dirname(fileURLToPath(import.meta.url));
const input = JSON.parse(await fs.readFile(path.join(baseDir, 'workbook_data.json'), 'utf8'));
const wb = Workbook.create();
const palette = { navy: '#203B5C', blue: '#376EAC', teal: '#2A8B83', amber: '#CB8A2D', text: '#243246', pale: '#EAF0F6', muted: '#66758A', red: '#B83B3B' };
const font = 'Arial';
const columns = n => { let s=''; for (let k=n;k>0;k=Math.floor((k-1)/26)) s=String.fromCharCode(65+(k-1)%26)+s; return s; };
const safe = v => v == null || (typeof v === 'number' && !Number.isFinite(v)) ? null : v;
const labels={factory:'공장',date:'날짜',pred_date:'날짜',target:'에너지원',model_group:'하위 모델',feature:'변수',description:'의미',position:'변수 순서',training_start:'학습 시작(기록)'};
const day = v => v ? new Date(String(v).slice(0,10)+'T00:00:00Z') : null;
const get = (o, names, fallback=null) => { for(const n of names) if(o[n]!==undefined) return o[n]; return fallback; };
function write(sheet, startRow, startCol, values) {
  if (!values.length || !values[0].length) return;
  sheet.getRangeByIndexes(startRow-1,startCol-1,values.length,values[0].length).values=values;
}
function baseSheet(name, color) {
  const sh=wb.worksheets.add(name); sh.showGridLines=false;
  if(color) sh.tabColor=color;
  return sh;
}
function table(sheet, row, headers, values, name, widths={}) {
  write(sheet,row,1,[headers,...values]);
  const last=row+values.length, end=columns(headers.length);
  const area=sheet.getRange(`A${row}:${end}${last}`);
  area.format.font={name:font,size:10,color:palette.text};
  area.format.rowHeight=21;
  area.format.verticalAlignment='center';
  area.format.columnWidth=15;
  const head=sheet.getRange(`A${row}:${end}${row}`);
  head.format={fill:palette.navy,font:{name:font,size:10,bold:true,color:'#FFFFFF'},horizontalAlignment:'center',verticalAlignment:'center',rowHeight:31};
  for(const [col,width] of Object.entries(widths)) sheet.getRange(`${col}${row}:${col}${last}`).format.columnWidth=width;
  if(values.length) {
    const t=sheet.tables.add(`A${row}:${end}${last}`,true,name);
    t.showFilterButton=true;
  }
  return {last,end};
}
function title(sheet, text, context, end='L') {
  sheet.getRange('A2').values=[[text]];
  sheet.getRange('A2').format.font={name:font,size:15,bold:true,color:palette.navy};
  sheet.getRange(`A3:${end}3`).format.borders={bottom:{style:'thin',color:palette.blue}};
  if(context){sheet.getRange('A4').values=[[context]];sheet.getRange('A4').format.font={name:font,size:10,italic:true,color:palette.muted};}
}
function numberFmt(sh, col, from, to, format='#,##0.00') { if(to>=from) sh.getRange(`${col}${from}:${col}${to}`).setNumberFormat(format); }
function addLine(sh, dataRange, name, start, end, colors) {
  const c=sh.charts.add('line',sh.getRange(dataRange));
  c.title=name;c.titleTextStyle.typeface=font;c.titleTextStyle.fontSize=13;
  c.legend={position:'top',textStyle:{typeface:font,fontSize:10}};
  c.xAxis={axisType:'textAxis',textStyle:{typeface:font,fontSize:10}};
  c.yAxis={numberFormatCode:'0.0"%"',numberFormatSourceLinked:false,textStyle:{typeface:font,fontSize:10}};
  c.setPosition(start,end);
  c.series.items.forEach((s,i)=>{s.line={fill:colors[i%colors.length],style:'solid',width:2};});
  return c;
}

const daily=input.daily ?? [];
if(!daily.length) throw new Error('workbook_data.json daily is empty');
const norm=daily.map(r=>({
  date:String(get(r,['date','pred_date'])).slice(0,10),factory:r.factory,target:r.target,
  unit:r.unit??'',pred:get(r,['predicted','pred_value','prediction','pred']),actual:get(r,['actual_value','actual']),
  p05:get(r,['pred_p05','p05']),p95:get(r,['pred_p95','p95']),
  workday:get(r,['is_workday'],null),split:get(r,['evaluation_split','split','period_type','training_overlap'],''),
  production:get(r,['mix_prod_kg','production_kg']),status:get(r,['status','data_status'],[r.zero_actual?'0실측, MAPE 제외':'',r.missing_actual?'실측 누락':'',r.missing_production?'생산량 누락':'',r.missing_weather_columns?'날씨 대체':''].filter(Boolean).join('; ')),
  special:r.is_special_event,webApe:r.web_ape_pct,mape7:r.mape_7d_pct,mape30:r.mape_30d_pct,n7:r.valid_ape_7d,n30:r.valid_ape_30d,
  missingProduction:r.missing_production,missingWeather:r.missing_weather_columns,drift:r.drift_adjustment,holiday:r.holiday_factor,basePred:r.base_model_prediction,
  error:r.error,absoluteError:r.absolute_error,ape:r.ape_pct,signedError:r.signed_error_pct
}));
norm.sort((a,b)=>a.factory.localeCompare(b.factory,'ko')||a.target.localeCompare(b.target,'ko')||a.date.localeCompare(b.date));

const summary=baseSheet('성능 요약',palette.navy);
const monthly=baseSheet('월별 추이',palette.blue);
const splitSheet=baseSheet('구간별 성능',palette.blue);
const recentSheet=input.recent_changes?.length?baseSheet('최근 성능 변화',palette.blue):null;
const details=baseSheet('일별 예측오차');
const featureSheet=baseSheet('일별 입력변수');
const specsSheet=baseSheet('모델 사용변수');
const methods=baseSheet('분석 방법','#8694A6');
const context=`${input.manifest?.date_from??norm[0].date} ~ ${input.manifest?.date_to??norm.at(-1).date} · 현재 웹 모델 재적용 · 단위별 별도 평가`;
title(summary,'5개 공장 예측 성능',context);
title(monthly,'월별 MAPE 추이','실측이 0인 날짜는 MAPE에서 제외합니다. 학습 포함 구간과 이후 구간을 구분해 해석하세요.');
title(splitSheet,'학습·검증·이후 구간별 성능','구간은 학습 소스의 기본 날짜 기준입니다. 실제 아티팩트 기록과 함께 확인하세요.');
title(details,'일별 예측 및 오차','오차 = 예측 − 실측. 양수는 과대예측, 음수는 과소예측입니다.');
title(featureSheet,'실제 일별 입력변수 파일','전체 입력값은 같은 결과 폴더의 CSV에 보존되어 있습니다.');
title(specsSheet,'공장·에너지원별 모델 사용변수','모델에 전달한 변수 목록과 하위 모델 구성을 기록합니다.');
title(methods,'평가 방법과 데이터 조건','결과를 해석하고 동일 조건으로 다시 계산하는 데 필요한 정의입니다.');

const dHeads=['날짜','공장','에너지원','단위','예측값','실측값','오차','절대오차','APE (%)','부호오차율 (%)','P05','P95','근무일','평가 구간','생산량 (kg)','데이터 상태','월','특별일','웹 APE (%)','7일 MAPE (%)','30일 MAPE (%)','7일 유효일','30일 유효일','생산량 누락','날씨 결측 열','드리프트 가산','휴일 보정배수','보정 전 예측','절대 실측값'];
const dRows=norm.map(r=>[day(r.date),r.factory,r.target,r.unit,safe(r.pred),safe(r.actual),safe(r.error),safe(r.absoluteError),safe(r.ape),safe(r.signedError),safe(r.p05),safe(r.p95),r.workday===null?null:(r.workday?'근무일':'비근무일'),r.split,safe(r.production),r.status,day(r.date.slice(0,7)+'-01'),r.special?'특별일':'일반일',safe(r.webApe),safe(r.mape7),safe(r.mape30),safe(r.n7),safe(r.n30),r.missingProduction?'누락':'존재',r.missingWeather??'',safe(r.drift),safe(r.holiday),safe(r.basePred),r.actual==null?null:Math.abs(r.actual)]);
const dTable=table(details,6,dHeads,dRows,'DailyPredictions',{A:13,B:12,C:11,D:9,E:16,F:16,G:16,H:16,I:13,J:17,K:16,L:16,M:13,N:24,O:18,P:25,Q:12,R:11,S:14,T:17,U:18,V:14,W:14,X:16,Y:55,Z:17,AA:17,AB:18,AC:18});
const dStart=7,dLast=dTable.last;
for(let i=0;i<3;i++){const row=7+i;if(norm[i].actual){details.getRange(`G${row}:J${row}`).formulas=[[`=E${row}-F${row}`,`=ABS(G${row})`,`=H${row}/ABS(F${row})*100`,`=G${row}/ABS(F${row})*100`]];}}
numberFmt(details,'A',7,dLast,'yyyy-mm-dd');numberFmt(details,'Q',7,dLast,'yyyy-mm');
for(const c of ['E','F','G','H','K','L','O'])numberFmt(details,c,7,dLast);
for(const c of ['I','J','S','T','U','Z','AA','AB'])numberFmt(details,c,7,dLast,'0.00');
details.freezePanes.freezeRows(6);details.freezePanes.freezeColumns(3);
details.getRange(`I7:I${dLast}`).conditionalFormats.add('cellIs',{operator:'greaterThan',formula:30,format:{fill:'#FDE8E7',font:{color:palette.red}}});
console.log('Daily values written');

const groups=[...new Map(norm.map(r=>[r.factory+'|'+r.target,{factory:r.factory,target:r.target,unit:r.unit}])).values()];
const summaryHeads=['공장','에너지원','단위','예측 건수','실측 건수','MAPE 유효일','MAPE (%)','WAPE (%)','평균오차','MAE','평가 대상'];
const scopes=[['regular_workdays','일반 근무일'],['workdays','근무일'],['all_calendar_days','전체 일자'],['nonworkdays','비근무일']];
const summaryGroups=scopes.flatMap(([key,label])=>groups.map(g=>({...g,scope:key,label})));
const inScope=(r,g)=>r.factory===g.factory&&r.target===g.target&&(g.scope==='all_calendar_days'||(g.scope==='nonworkdays'?!r.workday:g.scope==='workdays'?r.workday:r.workday&&!r.special));
const averageError=rows=>{const v=rows.filter(r=>r.error!=null);return v.length?v.reduce((sum,r)=>sum+r.error,0)/v.length:null;};
const summaryValues=summaryGroups.map(g=>{const s=input.summary.find(r=>r.factory===g.factory&&r.target===g.target&&r.scope===g.scope);const rows=norm.filter(r=>inScope(r,g));return[g.factory,g.target,g.unit,s.rows,rows.filter(r=>r.actual!=null).length,s.valid_ape,s.mape_pct,s.wape_pct,averageError(rows),s.mae,g.label];});
const sumTable=table(summary,7,summaryHeads,summaryValues,'ModelSummary',{A:12,B:11,C:9,D:14,E:14,F:15,G:15,H:15,I:17,J:17,K:20});
const dr=c=>`'일별 예측오차'!$${c}$${dStart}:$${c}$${dLast}`;
if(recentSheet){
 const recent=input.recent_changes;
 const period=recent[0];
 title(recentSheet,'일반 근무일의 최근 성능 변화','같은 길이의 60일 달력 구간에서 근무일 중 특별일을 제외해 비교합니다.');
 write(recentSheet,5,1,[['이전60일',day(period.date_from_previous60),day(period.date_to_previous60),null,null,'최근60일',day(period.date_from_recent60),day(period.date_to_recent60)]]);
 for(const c of ['B','C','G','H'])numberFmt(recentSheet,c,5,5,'yyyy-mm-dd');
 const recentRows=recent.map(r=>[r.factory,r.target,r.valid_ape_previous60,r.valid_ape_recent60,r.mape_pct_previous60,r.mape_pct_recent60,r.mape_pct_change_pp,r.mape_change_relative_pct,r.wape_pct_previous60,r.wape_pct_recent60,r.wape_pct_change_pp,r.bias_pct_previous60,r.bias_pct_recent60,r.bias_pct_change_pp]);
 const recentTable=table(recentSheet,7,['공장','에너지원','이전 유효일','최근 유효일','이전 MAPE (%)','최근 MAPE (%)','MAPE 변화 (%p)','MAPE 변화율 (%)','이전 WAPE (%)','최근 WAPE (%)','WAPE 변화 (%p)','이전 편향 (%)','최근 편향 (%)','편향 변화 (%p)'],recentRows,'RecentMetrics',{A:12,B:11,C:16,D:16,E:20,F:20,G:21,H:22,I:20,J:20,K:21,L:20,M:20,N:21});
 for(const c of ['E','F','G','H','I','J','K','L','M','N'])numberFmt(recentSheet,c,8,recentTable.last);
 recentSheet.getRange(`G8:G${recentTable.last}`).conditionalFormats.add('cellIs',{operator:'greaterThan',formula:0,format:{fill:'#FDE8E7',font:{color:palette.red}}});
 recentSheet.freezePanes.freezeRows(7);
}
for(const c of ['D','E','F'])numberFmt(summary,c,8,sumTable.last,'#,##0');
for(const c of ['G','H','I','J'])numberFmt(summary,c,8,sumTable.last);
summary.freezePanes.freezeRows(7);

const splits=[...new Set(norm.map(r=>r.split))];
const splitRows=groups.flatMap(g=>splits.map(s=>{const v=input.split.find(r=>r.factory===g.factory&&r.target===g.target&&r.evaluation_split===s);const rows=norm.filter(r=>r.factory===g.factory&&r.target===g.target&&r.split===s);return[g.factory,g.target,s,rows.filter(r=>r.actual!=null).length,v.valid_ape,v.mape_pct,v.wape_pct,averageError(rows)];}));
const splitTable=table(splitSheet,6,['공장','에너지원','평가 구간','실측 건수','MAPE 유효일','MAPE (%)','WAPE (%)','평균오차'],splitRows,'SplitMetrics',{A:13,B:12,C:25,D:16,E:17,F:17,G:17,H:18});
for(const c of ['F','G','H'])numberFmt(splitSheet,c,7,splitTable.last);
splitSheet.freezePanes.freezeRows(6);

const months=[...new Set(norm.map(r=>r.date.slice(0,7)))].sort();
const monthRows=groups.flatMap(g=>months.map(m=>{const v=input.monthly.find(r=>r.factory===g.factory&&r.target===g.target&&r.month===m);const rows=norm.filter(r=>r.factory===g.factory&&r.target===g.target&&r.date.startsWith(m));return[day(m+'-01'),g.factory,g.target,rows.filter(r=>r.actual!=null).length,v.valid_ape,v.mape_pct,v.wape_pct,averageError(rows),v.mape_change_pp,v.mape_change_ratio_pct];}));
const mTable=table(monthly,6,['월','공장','에너지원','실측 건수','MAPE 유효일','MAPE (%)','WAPE (%)','평균오차','MAPE 전월차 (%p)','MAPE 전월비 (%)'],monthRows,'MonthlyMetrics',{A:13,B:12,C:12,D:15,E:16,F:16,G:16,H:17,I:23,J:23});
numberFmt(monthly,'A',7,mTable.last,'yyyy-mm');for(const c of ['F','G','H','I','J'])numberFmt(monthly,c,7,mTable.last);
monthly.freezePanes.freezeRows(6);

const targets=[...new Set(groups.map(g=>g.target))];
const factoryOrder=[...new Set(groups.map(g=>g.factory))];
for(let t=0;t<targets.length;t++){
  const start=6+t*22;const end=start+months.length;
  write(monthly,start,28,[['월',...factoryOrder],...months.map(m=>[m,...factoryOrder.map(()=>null)])]);
  for(let mi=0;mi<months.length;mi++)for(let fi=0;fi<factoryOrder.length;fi++){
    const idx=monthRows.findIndex(v=>v[1]===factoryOrder[fi]&&v[2]===targets[t]&&v[0].toISOString().slice(0,7)===months[mi]);
    monthly.getRangeByIndexes(start+mi,28+fi,1,1).formulas=[[`='월별 추이'!F${7+idx}`]];
  }
  monthly.getRange(`AB${start}:${columns(28+factoryOrder.length)}${end}`).format.font={name:font,size:10,color:palette.text};
  addLine(monthly,`AB${start}:${columns(28+factoryOrder.length)}${end}`,`${targets[t]} 월별 MAPE (%)`,`L${start}`,`Z${start+19}`,[palette.blue,palette.teal,palette.amber,'#8B6EAE','#9D5B59']);
}
console.log('Summary and monthly values written');

const featureData=input.feature_inputs??input.feature_values??[];
table(featureSheet,6,['자료','파일','행 수','연결 기준'],[
 ['실제 일별 입력값','daily_model_inputs.csv',featureData.length,'factory + date + target'],
 ['모델별 사용변수','model_variables.csv',input.features?.length??null,'factory + target + model_group + feature'],
 ['일별 예측·오차','daily_predictions.csv',norm.length,'factory + date + target']
],'FeatureFileIndex',{A:23,B:39,C:15,D:57});
featureSheet.getRange('A11').values=[['변수 값의 공백은 해당 모델에서 그 변수를 사용하지 않음을 뜻합니다. 모델 사용변수 시트에서 변수 설명을 확인하세요.']];
console.log('Feature values written');
const featureSpecs=input.features??input.model_specs??[];
if(featureSpecs.length){
 const keys=[...new Set(featureSpecs.flatMap(Object.keys))];
 const rows=featureSpecs.map(r=>keys.map(k=>k==='training_start'?day(r[k]):safe(typeof r[k]==='object'?JSON.stringify(r[k]):r[k])));
 const sTable=table(specsSheet,6,keys.map(k=>labels[k]??k),rows,'ModelFeatures',{A:13,B:13,C:15,D:31,E:89,F:13,G:19});
 const trainingIndex=keys.indexOf('training_start');if(trainingIndex>=0)numberFmt(specsSheet,columns(trainingIndex+1),7,sTable.last,'yyyy-mm-dd');
 specsSheet.freezePanes.freezeRows(6);specsSheet.freezePanes.freezeColumns(Math.min(3,keys.length));
}
const notes=input.notes??[];
const methodRows=[
 ['평가 기준','현재 웹 활성 모델을 과거 날짜에 다시 적용한 결과입니다. 당시 발행된 예측 성능과 구분해야 합니다.'],
 ['저장 방식','일별·월별·이동창 지표는 검증한 실행 결과의 저장값입니다. 입력 셀을 수정해 모델을 다시 추론하는 파일은 아닙니다.'],
 ['MAPE','실측이 0이 아닌 날짜의 |예측−실측| / |실측| × 100의 평균입니다.'],
 ['WAPE','실측이 있는 날짜의 절대오차 합 / 절대 실측 합 × 100입니다. 단위가 다른 에너지원은 합산하지 않습니다.'],
 ['평균오차','예측−실측의 평균입니다. 양수는 과대예측, 음수는 과소예측입니다.'],
 ['사용량 단위','전력 kWh, 연료 Nm³, 용수 ton. 각 행의 단위를 확인하세요.'],
 ['학습·검증 구간','모델 파일에는 실제 학습종료일과 검증종료일이 기록되어 있지 않습니다. 구간명은 학습 소스의 기본 날짜에 따른 참고 구분이며, 확정된 미학습 검증 구간을 뜻하지 않습니다.'],
 ['생산·기상 조건','생산량과 날씨는 현재 확보한 해당 일자의 실측 자료입니다. 사전에 알 수 없었던 관측치를 포함하므로 실제 사전예측의 성능과 차이가 생길 수 있습니다.'],
 ['월별 비교','첫 달(2025-09)과 마지막 달(2026-10)은 부분 월입니다. MAPE의 전월차는 %p, 전월비는 변화율(%)입니다.'],
 ...notes.map(n=>Array.isArray(n)?n:[n.label??n.key??'조건',n.text??n.value??String(n)]),
];
table(methods,6,['항목','설명'],methodRows,'EvaluationNotes',{A:24,B:125});
methods.getRange(`B7:B${6+methodRows.length}`).format.wrapText=true;methods.getRange(`A7:B${6+methodRows.length}`).format.autofitRows();

console.log('Recalculating the small daily arithmetic sample');
wb.recalculate();
const reconciliations=[];
const comparison=(actual,expected,label)=>{
 if(expected==null){if(actual!==null&&actual!==''&&actual!==undefined)throw new Error(`Expected missing ${label}: ${actual}`);return;}
 if(typeof actual!=='number'||!Number.isFinite(actual)||Math.abs(actual-expected)>Math.max(1e-7,Math.abs(expected)*1e-9))throw new Error(`Workbook mismatch ${label}: ${actual} versus ${expected}`);
 reconciliations.push({label,absolute_difference:Math.abs(actual-expected)});
};
if(input.summary?.length){
 const actualSummary=summary.getRange(`A8:K${sumTable.last}`).values;
 summaryGroups.forEach((g,i)=>{const expected=input.summary.find(r=>r.factory===g.factory&&r.target===g.target&&r.scope===g.scope);if(!expected)throw new Error('Missing source summary group');comparison(actualSummary[i][6],expected.mape_pct,`${g.factory}/${g.target}/${g.scope}/MAPE`);comparison(actualSummary[i][7],expected.wape_pct,`${g.factory}/${g.target}/${g.scope}/WAPE`);comparison(actualSummary[i][9],expected.mae,`${g.factory}/${g.target}/${g.scope}/MAE`);});
}
if(input.monthly?.length){
 const actualMonths=monthly.getRange(`A7:J${mTable.last}`).values;
 monthRows.forEach((g,i)=>{const m=g[0].toISOString().slice(0,7);const expected=input.monthly.find(r=>r.factory===g[1]&&r.target===g[2]&&r.month===m);if(!expected)throw new Error('Missing source monthly group');comparison(actualMonths[i][5],expected.mape_pct,`${g[1]}/${g[2]}/${m}/MAPE`);comparison(actualMonths[i][6],expected.wape_pct,`${g[1]}/${g[2]}/${m}/WAPE`);});
}
if(recentSheet){
 const rows=recentSheet.getRange(`A8:N${7+input.recent_changes.length}`).values;
 input.recent_changes.forEach((r,i)=>{comparison(rows[i][4],r.mape_pct_previous60,`${r.factory}/${r.target}/previous60/MAPE`);comparison(rows[i][5],r.mape_pct_recent60,`${r.factory}/${r.target}/recent60/MAPE`);comparison(rows[i][6],r.mape_pct_change_pp,`${r.factory}/${r.target}/MAPE_change_pp`);});
}
const checks=await wb.inspect({kind:'table',range:'성능 요약!A7:K12',include:'values,formulas',tableMaxRows:6,tableMaxCols:11,maxChars:4500});
const arithmetic=details.getRange('I7:I9').values;arithmetic.forEach((r,i)=>comparison(r[0],norm[i].ape,`daily arithmetic ${i}`));
const errors=await wb.inspect({kind:'match',sheetId:'일별 예측오차',range:'G7:J9',searchTerm:'#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!',options:{useRegex:true,maxResults:10},maxChars:1500});
await fs.writeFile(path.join(baseDir,'workbook_checks.json'),JSON.stringify({summary:checks.ndjson,errors:errors.ndjson,reconciliations},null,2));
console.log(checks.ndjson);console.log(errors.ndjson);
const out=await SpreadsheetFile.exportXlsx(wb);
await out.save(path.join(baseDir,'prediction_performance_review.xlsx'));
console.log('XLSX exported');
const previewRanges=[['성능 요약','A1:K23'],['월별 추이','L5:Z25'],['구간별 성능','A1:H17'],['일별 예측오차','A1:J16'],['일별 입력변수','A1:D12'],['모델 사용변수','A1:G16'],['분석 방법',`A1:B${6+methodRows.length}`]];
if(recentSheet)previewRanges.push(['최근 성능 변화','A1:H23']);
for(let i=0;i<previewRanges.length;i++){
  const [sheetName,range]=previewRanges[i];const image=await wb.render({sheetName,range,scale:1.3,format:'png'});
  await fs.writeFile(path.join(baseDir,`workbook_preview_${i+1}.png`),new Uint8Array(await image.arrayBuffer()));
  console.log(`Rendered ${sheetName}`);
}
console.log(JSON.stringify({output:path.join(baseDir,'prediction_performance_review.xlsx'),dailyRows:norm.length,summaryRows:groups.length,monthlyRows:monthRows.length,featureRows:featureData.length,charts:targets.length}));
