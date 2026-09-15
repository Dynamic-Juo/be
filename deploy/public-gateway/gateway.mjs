import { createHmac } from 'node:crypto';
import { isIP } from 'node:net';

const json = (status, code, message) => new Response(JSON.stringify({error:{code,message,retryable:status>=500}}), {
  status, headers:{'content-type':'application/json','cache-control':'no-store'},
});

// Vercel Functions only: never deploy this behind an untrusted forwarding proxy.
export async function handlePublicRequest(request, env=process.env, fetcher=fetch) {
  if (env.PUBLIC_GATEWAY_ENABLED !== 'true') return json(503,'public_unavailable','공개 접수를 준비 중입니다.');
  const frontend = env.PUBLIC_FRONTEND_ORIGIN;
  const secret = env.GATEWAY_SHARED_SECRET;
  let origin;
  try {
    origin = new URL(env.PRIVATE_API_ORIGIN);
    if (origin.protocol !== 'https:' || origin.username || origin.password || origin.pathname!=='/' || origin.search || origin.hash) throw Error();
    if (new URL(frontend).origin !== frontend || !frontend.startsWith('https://')) throw Error();
  } catch { return json(503,'public_unavailable','공개 접수를 준비 중입니다.'); }
  if (!secret || secret.length<32 || !env.CF_ACCESS_CLIENT_ID || !env.CF_ACCESS_CLIENT_SECRET)
    return json(503,'public_unavailable','공개 접수를 준비 중입니다.');
  const url = new URL(request.url);
  const operation = url.searchParams.get('operation');
  const job = url.searchParams.get('job_id');
  const submit = request.method === 'POST' && operation === 'analyze';
  const read = request.method === 'GET' && operation === 'job' && /^[a-f0-9]{32}$/.test(job ?? '');
  if (!submit && !read) return json(404,'not_found','요청한 경로를 찾을 수 없습니다.');
  if ((request.headers.get('origin') && request.headers.get('origin')!==frontend)
      || (submit && request.headers.get('origin')!==frontend))
    return json(403,'origin_denied','허용되지 않은 요청입니다.');
  // Vercel overwrites this header. Reject lists; do not guess which hop is trustworthy.
  const ip = request.headers.get('x-forwarded-for') ?? '';
  if (!isIP(ip)) return json(400,'client_unavailable','요청 정보를 확인할 수 없습니다.');
  const clientId = createHmac('sha256',secret).update(`client-v1:${ip}`).digest('hex');
  const headers = {
    'content-type':'application/json', 'x-public-gateway-key':secret,
    'x-public-client-id':clientId, 'CF-Access-Client-Id':env.CF_ACCESS_CLIENT_ID,
    'CF-Access-Client-Secret':env.CF_ACCESS_CLIENT_SECRET,
  };
  let body;
  if (submit) {
    if (!(request.headers.get('content-type') ?? '').startsWith('application/json'))
      return json(415,'invalid_request','JSON 요청이 필요합니다.');
    const raw = await limitedText(request,8192);
    if (raw===null) return json(413,'request_too_large','요청이 너무 큽니다.');
    try {
      const data=JSON.parse(raw);
      if (!data || Array.isArray(data) || typeof data!=='object'
          || Object.keys(data).some(k=>!['url','session_id','turnstile_token'].includes(k))
          || typeof data.url!=='string' || data.url.length>2048
          || typeof data.turnstile_token!=='string' || !data.turnstile_token || data.turnstile_token.length>2048
          || (data.session_id!=null && (typeof data.session_id!=='string' || data.session_id.length>64))) throw Error();
      body=JSON.stringify(data);
    } catch { return json(422,'invalid_request','영상 주소와 요청 확인 정보를 확인해주세요.'); }
  } else {
    const token=request.headers.get('authorization') ?? '';
    if (!/^Bearer [0-9]{10}\.[a-f0-9]{64}$/.test(token)) return json(404,'not_found','분석 결과를 확인할 수 없습니다.');
    headers.authorization=token;
  }
  const controller=new AbortController();
  const timer=setTimeout(()=>controller.abort(),15000);
  try {
    const upstream=await fetcher(new URL(submit?'/api/analyze':`/api/jobs/${job}`,origin), {
      method:submit?'POST':'GET',headers,body,redirect:'manual',signal:controller.signal,
    });
    if (upstream.status>=300 && upstream.status<400 || !upstream.headers.get('content-type')?.includes('application/json'))
      return json(502,'upstream_unavailable','분석 서버 연결을 확인 중입니다.');
    const text=await limitedText(upstream,2*1024*1024);
    if (text===null) return json(502,'upstream_unavailable','분석 결과를 가져오지 못했습니다.');
    JSON.parse(text);
    const output={'content-type':'application/json','cache-control':'no-store'};
    for (const name of ['retry-after','x-request-id']) {
      const value=upstream.headers.get(name);
      if (value) output[name]=value;
    }
    return new Response(text,{status:upstream.status,headers:output});
  } catch { return json(502,'upstream_unavailable','분석 서버 연결을 확인 중입니다.'); }
  finally {clearTimeout(timer);}
}

async function limitedText(message,limit) {
  if (!message.body) return '';
  const reader=message.body.getReader(); const chunks=[];let size=0;
  try {
    while (true) {
      const {value,done}=await reader.read();if(done)break;
      size+=value.byteLength;
      if(size>limit){await reader.cancel();return null;}
      chunks.push(Buffer.from(value));
    }
    return Buffer.concat(chunks).toString('utf8');
  } finally {reader.releaseLock();}
}

// Copy into the FE repository as api/gateway.mjs; see vercel.json.example.
export default async function gateway(req,res) {
  let body;
  if (req.method==='POST') {
    if (req.body!==undefined) {
      body=typeof req.body==='string'?req.body:JSON.stringify(req.body);
    } else {
      const chunks=[];let size=0;
      for await (const chunk of req) {
        size+=Buffer.byteLength(chunk);
        if(size>8192){res.statusCode=413;res.end();return;}
        chunks.push(Buffer.from(chunk));
      }
      body=Buffer.concat(chunks);
    }
    if (Buffer.byteLength(body)>8192){res.statusCode=413;res.end();return;}
  }
  const headers=new Headers();
  for(const [key,value] of Object.entries(req.headers)) if(typeof value==='string')headers.set(key,value);
  const result=await handlePublicRequest(new Request(new URL(req.url,'https://gateway.invalid'),{method:req.method,headers,body}));
  res.statusCode=result.status;
  result.headers.forEach((value,key)=>res.setHeader(key,value));
  res.end(await result.text());
}
