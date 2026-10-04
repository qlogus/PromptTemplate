function bindVideoGeneration() {
    const promptId = state.current.id;
    const panel = document.createElement('section');
    panel.id = 'videoPanel';
    panel.className = 'section workflow-panel video-panel';
    panel.innerHTML = `<h2>使用当前 Prompt 生视频</h2><form class="stack" data-video-form>
        <div class="actions"><button type="button" class="workflow-shortcut" data-video-language="zh">使用中文 Prompt</button><button type="button" class="workflow-shortcut" data-video-language="en">Use English Prompt</button><button type="button" class="workflow-shortcut" data-video-language="ja">日本語 Prompt を使う</button></div>
        <label>视频 Provider<select class="field" name="provider"><option value="">加载中…</option></select></label>
        <label>生成模式<select class="field" name="mode"></select></label>
        <label>实际使用的 Prompt<textarea class="field" name="prompt" rows="8" required></textarea></label>
        <div data-video-references hidden></div>
        <div class="grid2"><label>宽度<input class="field" name="width" type="number" min="64" max="7680" step="8" value="1152" required></label><label>高度<input class="field" name="height" type="number" min="64" max="7680" step="8" value="768" required></label></div>
        <div class="grid2"><label>帧数<input class="field" name="num_frames" type="number" min="9" max="441" step="8" value="121" required></label><label>帧率<input class="field" name="frame_rate" type="number" min="1" max="60" value="24" required></label></div>
        <div class="muted" data-duration></div>
        <details><summary>更多参数</summary><div class="stack"><label>Seed<input class="field" name="seed" type="number" step="1"></label><label>推理步数<input class="field" name="num_inference_steps" type="number" min="1" max="200"></label><label>负向 Prompt<textarea class="field" name="negative_prompt" rows="3"></textarea></label></div></details>
        <button class="workflow-execute" data-submit-video disabled>生成视频</button>
        <div class="muted" role="status" data-video-message></div></form>
        <div class="video-jobs stack" data-video-jobs></div>`;
    $('editor').appendChild(panel);
    const form = panel.querySelector('form');
    const inputs = form.elements;
    const submit = panel.querySelector('[data-submit-video]');
    const message = panel.querySelector('[data-video-message]');
    const jobsContainer = panel.querySelector('[data-video-jobs]');
    const references = new GenerationReferences(panel.querySelector('[data-video-references]'));
    let providers = [];
    let timer;
    let disposed = false;
    const labels = {text:'文生视频',image:'单图生视频',keyframes:'关键帧视频'};
    const statuses = {pending:'等待提交',submitting:'正在提交',queued:'上游排队',running:'生成中',downloading:'正在缓存结果',completed:'生成完成',failed:'生成失败',unknown:'提交状态未知',download_failed:'下载失败',expired:'临时缓存已过期',cancelled:'已取消'};
    panel.querySelectorAll('[data-video-language]').forEach(button=>{
        button.onclick=()=>{inputs.prompt.value=$({zh:'contentZh',en:'contentEn',ja:'contentJa'}[button.dataset.videoLanguage]).value};
    });
    const updateMode = () => {
        const provider = providers.find(item=>item.id===inputs.provider.value);
        const mode = inputs.mode.value;
        const limit = mode==='text'?0:mode==='image'?1:provider?.video_settings.keyframe_limit || 0;
        references.setProvider(provider?{reference_protocol:'json_url',reference_limit:limit}:null);
        submit.disabled=!provider;
    };
    const loadProviders = async () => {
        const items = await api('/api/providers');
        if (disposed) return;
        const previous = inputs.provider.value;
        providers=items.filter(item=>item.provider_kind==='video');
        inputs.provider.innerHTML=providers.map(item=>`<option value="${item.id}" ${item.is_default?'selected':''}>${esc(item.name)} · ${esc(item.model)}${item.is_default?' · 全局默认':''}</option>`).join('')||'<option value="">请先添加视频 Provider</option>';
        if(providers.some(item=>item.id===previous))inputs.provider.value=previous;
        inputs.provider.onchange();
    };
    inputs.provider.onchange=()=>{
        const provider=providers.find(item=>item.id===inputs.provider.value);
        inputs.mode.innerHTML=(provider?.video_settings.modes||[]).map(mode=>`<option value="${mode}">${labels[mode]}</option>`).join('');
        updateMode();
    };
    inputs.mode.onchange=updateMode;
    const updateDuration=()=>{panel.querySelector('[data-duration]').textContent=`预计时长 ${(Number(inputs.num_frames.value)/Number(inputs.frame_rate.value)).toFixed(2)} 秒`};
    inputs.num_frames.oninput=inputs.frame_rate.oninput=updateDuration;
    updateDuration();
    const showJob = job => {
        let box=jobsContainer.querySelector(`[data-job="${job.id}"]`);
        if(!box){
            box=document.createElement('details');
            box.className='video-job';
            box.dataset.job=job.id;
            box.innerHTML='<summary></summary><div class="stack" data-job-content></div>';
            jobsContainer.appendChild(box);
        }
        box.querySelector('summary').textContent=`${statuses[job.status]} · ${job.provider.name} · ${job.provider.model} · ${new Date(job.created_at*1000).toLocaleString()}${job.saved_example_id?' · 已保存 Example':''}`;
        const content=box.querySelector('[data-job-content]');
        let status=content.querySelector('[data-job-status]');
        if(!status){status=document.createElement('div');status.dataset.jobStatus='';content.appendChild(status)}
        status.textContent=`${statuses[job.status]}${job.progress===null?'':` · ${job.progress}%`}${job.error?' · '+job.error:''}`;
        if(box.dataset.state===job.status && box.dataset.saved===(job.saved_example_id||''))return;
        box.dataset.state=job.status;box.dataset.saved=job.saved_example_id||'';
        content.querySelectorAll(':scope > :not([data-job-status])').forEach(node=>node.remove());
        const details=document.createElement('p');details.className='muted';details.textContent=`任务 ${job.remote_id||job.id} · 请求 ${job.request.width}×${job.request.height} · ${job.request.num_frames} 帧 / ${job.request.frame_rate} FPS`;
        content.appendChild(details);
        const submitted=document.createElement('details');
        const summary=document.createElement('summary');summary.textContent='实际提交的 Prompt 与参考图';
        const prompt=document.createElement('p');prompt.className='submitted-prompt';prompt.textContent=job.request.prompt;
        submitted.append(summary,prompt);
        job.request.references.forEach((source,index)=>{submitted.appendChild(mediaPreview({kind:'image',source},`图${index+1}`,{url:value=>value}))});
        content.appendChild(submitted);
        const actions=document.createElement('div');actions.className='actions';
        const button=(label,handler)=>{const node=document.createElement('button');node.type='button';node.className='secondary';node.textContent=label;node.onclick=async()=>{node.disabled=true;try{await handler();await loadJobs()}catch(error){message.textContent=error.message}finally{node.disabled=false}};actions.appendChild(node)};
        if(['queued','running','download_failed','expired'].includes(job.status))button(job.status==='download_failed'||job.status==='expired'?'重新获取结果':'更新状态',()=>api(`/api/video-jobs/${job.id}/refresh`,{method:'POST'}));
        if(job.status==='unknown'){
            const id=document.createElement('input');id.className='field';id.placeholder='填写上游任务 ID，恢复查询';id.maxLength=128;content.appendChild(id);
            button('恢复任务',()=>api(`/api/video-jobs/${job.id}/recover`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({remote_id:id.value.trim()})}));
        }
        if(job.status==='completed'){
            const player=document.createElement('video');player.controls=true;player.preload='metadata';player.className='media-player';player.src=job.preview_url;content.appendChild(player);
            const actual=job.metadata.actual;
            if(actual?.width){const text=document.createElement('span');text.className='muted';text.textContent=`实际 ${actual.width}×${actual.height}${actual.duration?' · '+Number(actual.duration).toFixed(2)+' 秒':''}`;content.appendChild(text)}
            const link=document.createElement('a');link.href=job.preview_url;link.target='_blank';link.textContent='打开临时视频';content.appendChild(link);
            if(!job.saved_example_id){
                const save=document.createElement('form');save.className='stack';save.innerHTML=`<label>Example 标题<input class="field" name="title" value="视频 Example" required></label><label>生成模型 / Workflow ID<input class="field" name="model"></label><label>评分<input class="field" name="rating" type="number" min="0" max="5" value="0"></label><label>说明<textarea class="field" name="notes" rows="3"></textarea></label><label><input type="checkbox" name="compress"> 另存压缩副本，保留原视频</label><button class="workflow-shortcut">保存为 Example</button><span class="muted" role="status"></span>`;
                save.elements.model.value=job.provider.model;
                save.onsubmit=async event=>{event.preventDefault();const btn=save.querySelector('button');btn.disabled=true;save.querySelector('[role=status]').textContent='保存资源中…';try{await api(`/api/video-jobs/${job.id}/example`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:save.elements.title.value,generator_model:save.elements.model.value,rating:Number(save.elements.rating.value),notes:save.elements.notes.value,compress:save.elements.compress.checked})});if(state.current?.id===promptId&&!disposed)await open(promptId)}catch(error){save.querySelector('[role=status]').textContent=error.message;btn.disabled=false}};
                content.appendChild(save);
            }
        }
        if(!['pending','submitting','queued','running','downloading'].includes(job.status))button('清除本地任务记录',async()=>{if(!confirm('清除临时视频与本地任务记录？已保存的 Example 会保留。'))return;await api(`/api/video-jobs/${job.id}`,{method:'DELETE'});box.remove()});
        content.appendChild(actions);
    };
    const loadJobs = async () => {
        clearTimeout(timer);
        try{
            const jobs=await api('/api/video-jobs?'+new URLSearchParams({prompt_id:promptId}));
            if(disposed)return;
            jobsContainer.querySelectorAll('[data-job]').forEach(node=>{if(!jobs.some(job=>job.id===node.dataset.job))node.remove()});
            jobs.forEach(showJob);
            if(jobs.some(job=>['pending','submitting','queued','running','downloading'].includes(job.status)))timer=setTimeout(loadJobs,5000);
        }catch(error){if(!disposed){message.textContent=error.message;timer=setTimeout(loadJobs,15000)}}
    };
    form.onsubmit=async event=>{
        event.preventDefault();submit.disabled=true;message.textContent='正在创建任务…';
        try{
            const request={prompt_id:promptId,provider_id:inputs.provider.value,prompt:inputs.prompt.value,mode:inputs.mode.value,references:await references.snapshot(),negative_prompt:inputs.negative_prompt.value};
            for(const key of ['width','height','num_frames','frame_rate'])request[key]=Number(inputs[key].value);
            for(const key of ['seed','num_inference_steps'])if(inputs[key].value!=='')request[key]=Number(inputs[key].value);
            const job=await api('/api/video-jobs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(request)});
            if(disposed)return;
            showJob(job);jobsContainer.querySelector(`[data-job="${job.id}"]`).open=true;message.textContent='任务已创建，可以刷新页面或切换条目。';await loadJobs();
        }catch(error){message.textContent=error.message;await loadJobs()}finally{submit.disabled=!inputs.provider.value}
    };
    panel.addEventListener('generation-dispose',()=>{disposed=true;clearTimeout(timer);references.dispose()},{once:true});
    panel.addEventListener('providers-updated',()=>loadProviders().catch(error=>{message.textContent=error.message}));
    loadProviders().catch(error=>{message.textContent=error.message});
    loadJobs();
}
