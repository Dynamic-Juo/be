import test from 'node:test';
import assert from 'node:assert/strict';
import { handlePublicRequest } from './gateway.mjs';

const env = {
  PUBLIC_GATEWAY_ENABLED: 'true', PUBLIC_FRONTEND_ORIGIN: 'https://chamsae-ai.vercel.app',
  PRIVATE_API_ORIGIN: 'https://api.example.com', GATEWAY_SHARED_SECRET: 'a'.repeat(64),
  CF_ACCESS_CLIENT_ID: 'server-id', CF_ACCESS_CLIENT_SECRET: 'server-secret',
};
const payload = {url:'https://youtu.be/cYRkZmBuDqI', turnstile_token:'one-time-token'};
function request({method='POST', query='operation=analyze', body=payload, headers={}}={}) {
  return new Request(`https://chamsae-ai.vercel.app/api/gateway?${query}`, {
    method, headers:{origin:env.PUBLIC_FRONTEND_ORIGIN, 'content-type':'application/json',
      'x-forwarded-for':'203.0.113.12', ...headers},
    body:method==='POST'?JSON.stringify(body):undefined,
  });
}
const noFetch = () => assert.fail('rejected request reached private origin');

test('disabled and incomplete configuration fail closed', async () => {
  assert.equal((await handlePublicRequest(request(), {}, noFetch)).status,503);
  assert.equal((await handlePublicRequest(request(), {...env,CF_ACCESS_CLIENT_SECRET:''},noFetch)).status,503);
});
test('only two operations and expected origin are exposed', async () => {
  for(const query of ['operation=health','operation=internal/deployment/resume','operation=job&job_id=../health'])
    assert.equal((await handlePublicRequest(request({query}),env,noFetch)).status,404);
  for(const origin of ['', 'https://evil.example'])
    assert.equal((await handlePublicRequest(request({headers:{origin}}),env,noFetch)).status,403);
});
test('high cost options, oversized bodies and ambiguous client identity are rejected', async () => {
  assert.equal((await handlePublicRequest(request({body:{...payload,max_frames:64}}),env,noFetch)).status,422);
  assert.equal((await handlePublicRequest(request({body:{...payload,url:'a'.repeat(9000)}}),env,noFetch)).status,413);
  assert.equal((await handlePublicRequest(request({headers:{'x-forwarded-for':'1.1.1.1, 2.2.2.2'}}),env,noFetch)).status,400);
});
test('gateway mints identity, strips cookies, forwards only fixed path and hides upstream headers', async () => {
  let called=false;
  const response=await handlePublicRequest(request({headers:{cookie:'private-cookie',
    'x-public-client-id':'spoofed','CF-Access-Client-Secret':'spoofed'}}),env,async (url,options)=>{
    called=true;
    assert.equal(String(url),'https://api.example.com/api/analyze');
    assert.equal(options.redirect,'manual');
    assert.equal(options.headers['CF-Access-Client-Secret'],'server-secret');
    assert.match(options.headers['x-public-client-id'],/^[a-f0-9]{64}$/);
    assert.equal(options.headers.cookie,undefined);
    assert.equal(options.headers['x-forwarded-for'],undefined);
    assert.deepEqual(JSON.parse(options.body),payload);
    return new Response('{"job_id":"ok"}',{status:202,headers:{'content-type':'application/json',
      'set-cookie':'secret=value','CF-Access-Client-Secret':'secret','x-request-id':'request-1'}});
  });
  assert.ok(called);
  assert.equal(response.status,202);
  assert.equal(response.headers.get('cache-control'),'no-store');
  assert.equal(response.headers.get('set-cookie'),null);
  assert.equal(response.headers.get('CF-Access-Client-Secret'),null);
  assert.equal(response.headers.get('x-request-id'),'request-1');
});
test('Access redirects and non-JSON fail without leaking login content', async () => {
  for(const upstream of [new Response('login',{status:302,headers:{location:'https://login.example'}}),
    new Response('<html>login</html>',{headers:{'content-type':'text/html'}})]) {
    const response=await handlePublicRequest(request(),env,async()=>upstream);
    assert.equal(response.status,502);
    assert.equal(response.headers.get('location'),null);
    assert.ok(!(await response.text()).includes('login.example'));
  }
});
test('job requests require capability and preserve retry-after', async () => {
  const job='a'.repeat(32), query=`operation=job&job_id=${job}`;
  assert.equal((await handlePublicRequest(request({method:'GET',query}),env,noFetch)).status,404);
  const token=`Bearer 1800000000.${'b'.repeat(64)}`;
  const response=await handlePublicRequest(request({method:'GET',query,headers:{authorization:token}}),env,async(url,options)=>{
    assert.equal(String(url),`https://api.example.com/api/jobs/${job}`);
    assert.equal(options.headers.authorization,token);
    return new Response('{"error":{"code":"public_quota_exceeded"}}',{
      status:429,headers:{'content-type':'application/json','retry-after':'60'}});
  });
  assert.equal(response.status,429);
  assert.equal(response.headers.get('retry-after'),'60');
});
