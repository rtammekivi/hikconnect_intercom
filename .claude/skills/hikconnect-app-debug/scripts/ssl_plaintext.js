'use strict';
const seen = new Set();
function dump(tag, p, n) {
  if (n <= 0) return;
  let s; try { s = p.readUtf8String(Math.min(n, 4096)); } catch (e) { try { s = p.readCString(Math.min(n, 4096)); } catch (e2) { return; } }
  if (!s || !/(\/v3\/|ISAPI|callSignal|devconfig|calling|HTTP\/1\.1 200)/.test(s)) return;
  console.log('==== ' + tag + ' ' + new Date().toISOString() + '\n' + s.replace(/sessionId: [^\r\n]+/g, 'sessionId: <redacted>'));
}
function hookAll() {
  for (const m of Process.enumerateModules()) {
    for (const name of ['SSL_write', 'SSL_read']) {
      let a = null;
      try { a = m.findExportByName(name); } catch (e) {}
      if (!a || seen.has(a.toString())) continue;
      seen.add(a.toString());
      console.log('hook ' + m.name + '!' + name);
      if (name === 'SSL_write') Interceptor.attach(a, { onEnter(args) { dump('SEND ' + m.name, args[1], args[2].toInt32()); } });
      else Interceptor.attach(a, { onEnter(args) { this.b = args[1]; }, onLeave(r) { dump('RECV ' + m.name, this.b, r.toInt32()); } });
    }
  }
}
hookAll();
setInterval(hookAll, 5000);
