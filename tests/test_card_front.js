'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('web_static/app.js','utf8').split("$('password-toggle').onclick=")[0];
const context=vm.createContext({console,URLSearchParams,document:{getElementById:()=>({hidden:true}),querySelectorAll:()=>[]}});
vm.runInContext(source+`\nglobalThis.test={state,commentEditor,cardSaveEditor,bindCardEditors,taskCard,classificationEditor,clientFieldsEditor,warnUnsavedCards,bankKeyStatus}; bindOfferFiles=()=>{}; refreshCards=async()=>true; savedNotice=()=>{}; notice=()=>{};`,context);
const {state,commentEditor,cardSaveEditor,bindCardEditors,taskCard,classificationEditor,clientFieldsEditor,warnUnsavedCards,bankKeyStatus}=context.test;
const item={term_id:1,date:'2026-11-18',version:3,public_text:{revision:4},client_comment:{revision:5,text:'старое'},operator_review:{revision:6},terms:{groups:[]}};
assert.match(commentEditor(item),/<textarea/);
assert.doesNotMatch(commentEditor(item),/<details/);
assert.equal((cardSaveEditor(item).match(/>OK</g)||[]).length,1);
assert.match(cardSaveEditor(item),/data-card-save="1" disabled/);
assert.match(classificationEditor(item,'search'),/🟢 Зелёный/);assert.match(classificationEditor(item,'search'),/<legend>Выберите группы<\/legend>/);assert.doesNotMatch(classificationEditor(item,'search'),/<details class="classification-details"/);
let prevented=false;const leave={preventDefault(){prevented=true},returnValue:undefined};warnUnsavedCards(leave);assert.equal(prevented,false);state.commentDrafts.set('draft','ввод');warnUnsavedCards(leave);assert.equal(prevented,true);assert.equal(leave.returnValue,'');state.commentDrafts.clear();
const displayItem={...item,company:'Пример',inn:'123',source_chat:'Источник',company_vat:{value:'без НДС'},terms:{groups:[],color:'green'},public_text:{revision:1,preview:'Сохранённые условия'},client_comment:{text:'Комментарий'},documents:[{name:'счёт.pdf',share_selected:true}]};
const collapsed=taskCard(displayItem,true);assert.match(collapsed,/Сохранённые условия/);assert.match(collapsed,/Комментарий/);assert.match(collapsed,/счёт.pdf \(ссылка пока недоступна\)/);assert.match(collapsed,/🟢 зелёный/);assert.match(collapsed,/data-card-details="search:2026-11-18:1"\s*>/);
assert.match(taskCard({...displayItem,public_text:{},client_fields:{subject:'Поставка'}},true),/Назначение:<\/b> Поставка/);assert.match(collapsed,/<summary class="card-summary">/);
// Only the saved comment appears in the summary; editing and saving stay inside the disclosure.
for(const searchable of [true,false]){
 const input={...displayItem,public_text:{revision:1,preview:'Начало\nСветофор: 🟢 зелёный\nКомментарий: текст в клиентском тексте'},client_comment:{revision:5,text:'Первая строка\n<вторая>'}};
 state.queueCards.set('1',input);
 const html=taskCard(input,searchable), summary=html.slice(html.indexOf('<summary class="card-summary">'),html.indexOf('</summary>'));
 assert.equal((html.match(/data-card-edit=/g)||[]).length,1);
 assert.equal((html.match(/data-comment-text/g)||[]).length,1);
 assert.equal((html.match(/data-card-save=/g)||[]).length,1);
 assert.ok(html.indexOf('data-comment-text')<html.lastIndexOf('</details>'));
 assert.ok(html.indexOf('data-comment-text')>html.indexOf('</summary>'));
 assert.ok(html.indexOf('data-card-save=')>html.indexOf('data-comment-text'));
 assert.doesNotMatch(summary,/data-comment-text|data-card-save|Сохранённый комментарий/);
 assert.match(summary,/<b>Комментарий:<\/b> Первая строка\n&lt;вторая&gt;/);
 const emptySummary=taskCard({...input,client_comment:{text:''}},true).split('</summary>')[0];
 assert.doesNotMatch(emptySummary,/<b>Комментарий:<\/b>/);
 assert.match(html,/Первая строка\n&lt;вторая&gt;/);
 assert.match(summary,/Комментарий: текст в клиентском тексте/);
 assert.match(summary,/Светофор: 🟢 зелёный/);
 assert.match(summary,/Комментарий в тексте отличается от сохранённого комментария/);
 const matching={...input,public_text:{revision:1,preview:'Условия\nСветофор: 🟢 зелёный\nКомментарий: Первая строка\n<вторая>'}};
 state.queueCards.set('1',matching);
 const matchingSummary=taskCard(matching,searchable).split('</summary>')[0];
 assert.equal((matchingSummary.match(/Первая строка/g)||[]).length,1);
 assert.match(matchingSummary,/Комментарий: Первая строка\n&lt;вторая&gt;/);
 assert.doesNotMatch(matchingSummary,/Комментарий в тексте отличается/);
 assert.equal(matching.public_text.preview,'Условия\nСветофор: 🟢 зелёный\nКомментарий: Первая строка\n<вторая>');
 const prefix={...input,public_text:{revision:1,preview:'Комментарий: Первая строка\n<вторая> лишнее'}};
 state.queueCards.set('1',prefix);
 const prefixSummary=taskCard(prefix,searchable).split('</summary>')[0];
 assert.match(prefixSummary,/Комментарий в тексте отличается/);
 assert.equal((prefixSummary.match(/Первая строка/g)||[]).length,2);
 for(const [saved,existing] of [['А','А\nдругая строка'],['А\nдругая строка','А'],['А','АБ'],['АБ','А']]){
  const differing={...input,client_comment:{text:saved},public_text:{revision:1,preview:'Условия\nКомментарий: '+existing}};
  state.queueCards.set('1',differing);
  const differingSummary=taskCard(differing,searchable).split('</summary>')[0];
  assert.match(differingSummary,/Комментарий в тексте отличается/);
  assert.ok(differingSummary.includes('<b>Комментарий:</b> '+saved));
 }
 const indented={...input,client_comment:{text:'А'},public_text:{revision:1,preview:'  Комментарий: А'}};
 state.queueCards.set('1',indented);
 assert.match(taskCard(indented,searchable).split('</summary>')[0],/Комментарий в тексте отличается/);
 assert.equal(input.public_text.preview,'Начало\nСветофор: 🟢 зелёный\nКомментарий: текст в клиентском тексте');
 const manual={...input,public_text:{revision:1,preview:'Комментарий: первая строка\nСветофор: 🟢 зелёный'}};
 state.queueCards.set('1',manual);
 const manualHtml=taskCard(manual,searchable);assert.ok(manualHtml.includes(manual.public_text.preview));assert.doesNotMatch(manualHtml,/Светофор в тексте отличается/);
 const auto={...input,public_text:{revision:null,generated_signal:true,preview:'Условия\nСветофор: 🟢 зелёный'}};
 state.queueCards.set('1',auto);
 const autoHtml=taskCard(auto,searchable);assert.doesNotMatch(autoHtml,/Светофор: 🟢 зелёный/);assert.equal(auto.public_text.preview,'Условия\nСветофор: 🟢 зелёный');
 const savedAuto={...auto,public_text:{...auto.public_text,revision:22}};state.queueCards.set('1',savedAuto);assert.doesNotMatch(taskCard(savedAuto,searchable),/Светофор: 🟢 зелёный/);
 for(const [platform,label] of [['telegram','Telegram'],['whatsapp','WhatsApp'],[null,'Мессенджер не указан']]){const withPlatform={...input,source_platform:platform};state.queueCards.set('1',withPlatform);const output=taskCard(withPlatform,searchable);assert.ok(output.includes('Источник:</b> Источник · '+label));assert.equal((output.match(/Чат-источник:/g)||[]).length,1);}
 for(const line of ['Светофор: 🟡 жёлтый','Светофор: не указан','Светофор: 🟢 зелёный с оговоркой']){
  const altered={...input,public_text:{revision:1,preview:'Условия\n'+line}};
  state.queueCards.set('1',altered);
  const result=taskCard(altered,searchable);assert.ok(result.includes(line));assert.match(result,/Светофор в тексте отличается/);
 }
}
assert.equal(bankKeyStatus('044525225','40702810200044525225'),'valid');
assert.equal(bankKeyStatus('044525225','40702810200000000002'),'invalid');
assert.equal(bankKeyStatus('044525225',''),'unchecked');
assert.equal(bankKeyStatus('044525225','123'),'unchecked');
assert.equal(bankKeyStatus('044525225','30101810400000000225'),'unsupported');
for(const searchable of [true,false]){
 const bank={...displayItem,client_fields:{bik:'044525225',account:'40702810200044525225'}};
 state.queueCards.set('1',bank);
 const html=taskCard(bank,searchable),summary=html.split('</summary>')[0];
 assert.match(summary,/БИК \(внутренние сведения\):<\/b> 044525225/);
 assert.match(summary,/Р\/С \(внутренние сведения\):<\/b> 40702810200044525225/);
 assert.match(summary,/Ключ счёта кредитной организации:<\/b> сошёлся/);
 assert.match(html,/Ключ счёта кредитной организации: сошёлся/);
 assert.match(html,/data-client-field="bik"[^>]*value="044525225"/);
 assert.match(html,/data-client-field="account"[^>]*value="40702810200044525225"/);
 state.clientFieldsDrafts.set('2026-11-18:1',{account:'40702810200000000002'});
 const draft=taskCard(bank,searchable);assert.match(draft.split('</summary>')[0],/Ключ счёта кредитной организации:<\/b> не сошёлся/);assert.match(draft,/Ключ счёта кредитной организации: не сошёлся/);
 state.clientFieldsDrafts.clear();
 const noBank={...bank,client_fields:{}};state.queueCards.set('1',noBank);
 const empty=taskCard(noBank,searchable).split('</summary>')[0];
 assert.doesNotMatch(empty,/БИК \(внутренние сведения\)|Р\/С \(внутренние сведения\)/);assert.match(empty,/Ключ счёта кредитной организации:<\/b> не проверен/);
}
state.queueCards.clear();
const missingCard=taskCard(displayItem,false);
assert.doesNotMatch(missingCard,/data-card-edit=|data-comment-text|data-card-save=/);
assert.match(missingCard,/Редактирование недоступно/);
state.commentDrafts.set('2026-11-18:1','Черновик');
const draftHtml=taskCard(displayItem,true);
assert.match(draftHtml,/<textarea[^>]*>Черновик<\/textarea>/);
assert.match(draftHtml,/Сохранённый комментарий: Комментарий/);
const draftSummary=draftHtml.split('</summary>')[0];assert.match(draftSummary,/<b>Комментарий:<\/b> Комментарий/);assert.doesNotMatch(draftSummary,/Черновик/);
state.commentDrafts.clear();
state.clientFieldsDrafts.set('2026-11-18:1',{subject:'Мой ввод',final_rate:'10',bik:'044525225',account:'40702810200044525225'});state.cardCompare.add('2026-11-18:1');const compared=clientFieldsEditor({...item,client_fields:{subject:'Сервер',final_rate:12,bik:'123456789',account:'12345678901234567890'}});assert.match(compared,/value="Мой ввод"/);assert.match(compared,/назначение — Сервер; цена для передачи — 12/);assert.match(compared,/БИК — 123456789; Р\/С — 12345678901234567890/);assert.match(compared,/value="044525225"/);state.classificationDrafts.set('2026-11-18:1',{groups:['Черновик'],groups_changed:true});state.groups=['Черновик','Сервер'];const groupsCompared=classificationEditor({...item,terms:{groups:['Сервер']}},'search');assert.match(groupsCompared,/Текущие группы на сервере:<\/strong> Сервер/);assert.match(groupsCompared,/value="Черновик" checked/);assert.match(groupsCompared,/value="Сервер"\s*>/);state.classificationDrafts.clear();state.groups=[];state.clientFieldsDrafts.clear();state.cardCompare.clear();
state.expandedCards.add('search:2026-11-18:1');assert.match(taskCard(displayItem,true),/data-card-details="search:2026-11-18:1" open/);state.expandedCards.clear();
const control=(extra={})=>Object.assign({disabled:false,hidden:false,dataset:{},value:'',checked:false,isConnected:true,textContent:'',matches:()=>false},extra);
function setup(){
 const save=control(),status=control(),summary=control(),error=control(),refresh=control(),vat=control(),comment=control(),review=control(),color=control(),changed=control(),bankBrief=control(),bankStatus=control();
 const colorConfirm=control(), groupConfirm=control(),group=control({value:'A',checked:false});const classification={querySelector:s=>({'[data-classification-color]':color,'[data-color-changed]':changed,'[data-color-confirm]':colorConfirm,'[data-classification-confirm]':groupConfirm}[s]||null),querySelectorAll:()=>group.checked?[group]:[]};
 const field=control({dataset:{clientField:'subject'},value:'Новое'}),bik=control({dataset:{clientField:'bik'}}),account=control({dataset:{clientField:'account'}});
 const selector={'[data-bank-brief]':bankBrief,'[data-bank-editor-status]':bankStatus,'[data-card-save]':save,'[data-card-status]':status,'[data-card-error]':error,'[data-card-refresh]':refresh,'[data-vat-select]':vat,'[data-comment-text]':comment,'[data-review]':review,'.classification':classification};
 const editor={dataset:{cardEdit:'1'},isConnected:true,closest:()=>({querySelector:()=>summary}),querySelector:s=>selector[s]||null,querySelectorAll:s=>s==='[data-client-field]'?[field,bik,account]:[field,bik,account,vat,comment,review,color,changed]};
 context.document.querySelectorAll=()=>[editor];
 const root={querySelectorAll:s=>s==='[data-card-edit]'?[editor]:[]};bindCardEditors(root,[item]);
 return {save,status,summary,error,refresh,vat,comment,review,field,bik,account,classification,color,colorConfirm,group,groupConfirm,editor,bankBrief,bankStatus};
}
(async()=>{
 const invalid=setup();let calls=0;context.fetch=()=>{calls++;throw Error('request should not be sent');};
 for(const [field,value] of [[invalid.bik,'123'],[invalid.bik,'１２３４５６７８９'],[invalid.account,'0'.repeat(19)],[invalid.account,'0'.repeat(19)+'x']]){
  field.value=value;field.oninput();await invalid.save.onclick();assert.equal(calls,0);assert.match(invalid.error.textContent,/БИК должен содержать 9 цифр, расчётный счёт — 20 цифр/);assert.equal(invalid.error.hidden,false);assert.equal(invalid.save.disabled,false);assert.equal(state.cardConflicts.has(item.date+':1'),false);assert.equal(state.clientFieldsDrafts.get(item.date+':1')[field.dataset.clientField],value);field.value='';field.oninput();
 }
 assert.equal(state.clientFieldsDrafts.has(item.date+':1'),false);
 let resolve;let payload;context.fetch=(url,options)=>{payload=JSON.parse(options.body);return new Promise(r=>resolve=r);};
 const a=setup();a.field.oninput();a.bik.value='044525225';a.bik.oninput();a.account.value='40702810200044525225';a.account.oninput();a.vat.value='без НДС';a.vat.onchange({target:a.vat});a.comment.value='текст';a.comment.oninput({target:a.comment});a.review.checked=true;a.review.onchange();
 assert.match(a.bankStatus.textContent,/сошёлся/);a.account.value='40702810200000000002';a.account.oninput();assert.match(a.bankStatus.textContent,/не сошёлся/);assert.match(a.bankBrief.innerHTML,/не сошёлся/);a.account.value='40702810200044525225';a.account.oninput();
 const pending=a.save.onclick();assert.equal(a.comment.disabled,true);assert.equal(a.save.disabled,true);assert.deepEqual(JSON.parse(JSON.stringify(payload)),{date:item.date,version:3,public_revision:4,comment_revision:5,review_revision:6,changes:{client_fields:{subject:'Новое',bik:'044525225',account:'40702810200044525225'},vat_condition:'без НДС',client_comment:'текст',review:true}});
 resolve({ok:false,status:409,json:async()=>({error:'конфликт'})});await pending;
 assert.equal(state.clientFieldsDrafts.get(item.date+':1').bik,'044525225');assert.equal(state.clientFieldsDrafts.get(item.date+':1').account,'40702810200044525225');assert.equal(state.commentDrafts.get(item.date+':1'),'текст');assert.equal(a.save.disabled,true);assert.equal(a.refresh.hidden,false);assert.match(a.summary.textContent,/Данные изменились/);a.comment.value='новый ввод';a.comment.oninput({target:a.comment});assert.match(a.summary.textContent,/Данные изменились/);assert.equal(a.save.disabled,true);
 state.cardConflicts.clear();const b=setup();let resolve2;context.fetch=()=>new Promise(r=>resolve2=r);const pending2=b.save.onclick();state.commentDrafts.set(item.date+':1','новее');resolve2({ok:true,json:async()=>({})});await pending2;assert.equal(state.commentDrafts.get(item.date+':1'),'новее');assert.equal(state.vatDrafts.has(item.date+':1'),false);

 state.commentDrafts.clear();state.cardSaved.clear();const c=setup(),key=item.date+':1';
 c.field.value='изменено';c.field.oninput();assert.equal(c.save.disabled,false);c.field.value='';c.field.oninput();assert.equal(c.save.disabled,true);assert.equal(state.clientFieldsDrafts.has(key),false);
 c.vat.value='без НДС';c.vat.onchange({target:c.vat});c.vat.value='';c.vat.onchange({target:c.vat});assert.equal(state.vatDrafts.has(key),false);
 c.comment.value='новый';c.comment.oninput({target:c.comment});c.comment.value='старое';c.comment.oninput({target:c.comment});assert.equal(state.commentDrafts.has(key),false);
 c.review.checked=true;c.review.onchange();c.review.checked=false;c.review.onchange();assert.equal(state.reviewDrafts.has(key),false);
 c.color.value='green';c.classification.onchange();c.color.value='';c.classification.onchange();assert.equal(state.classificationDrafts.has(key),false);
 c.group.checked=true;c.classification.onchange();c.group.checked=false;c.classification.onchange();assert.equal(state.classificationDrafts.has(key),false);
 c.colorConfirm.onclick();c.groupConfirm.onclick();assert.equal(state.classificationDrafts.get(key).color_changed,true);assert.equal(state.classificationDrafts.get(key).groups_changed,true);
 state.classificationDrafts.clear();state.classificationConfirms.clear();c.comment.value='ошибка';c.comment.oninput({target:c.comment});
 let rejectResponse;context.fetch=()=>new Promise(r=>rejectResponse=r);let output='';context.document.getElementById=id=>({hidden:id!=='search-view'});context.capture=s=>{output=s};vm.runInContext("renderSearch=()=>capture(cardSaveEditor("+JSON.stringify(item)+"));",context);
 const pending3=c.save.onclick();c.editor.isConnected=false;rejectResponse({ok:false,status:409,json:async()=>({error:'конфликт'})});await pending3;assert.match(output,/data-card-refresh(?! hidden)/);assert.match(output,/конфликт/);assert.match(output,/data-card-save="1" disabled/);
 console.log('card frontend tests passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
