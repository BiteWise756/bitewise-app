// auth_routing.js - SINGLE SOURCE OF TRUTH for auth routing.
// Loaded by Sign.html, bitewise-app.html and bitewise-app(2).html.
//
// Preserved localStorage keys (never renamed):
//   bw_uid, bw_accountType, bw_premium_{uid}, bw_doctorPremium_{uid}
//
// Routing rules:
//   not logged in          -> Sign.html
//   bw_doctorPremium_*     -> medical_dashboard_v2.html
//   bw_accountType=doctor  -> medical_dashboard_v2(2).html
//   bw_premium_{uid}=true  -> bitewise-app(2).html
//   anything else          -> bitewise-app.html
//
// Loop prevention (requirement 7): redirectToLanding() compares the current
// pathname with the destination and does nothing when they match. Because the
// landing decision is deterministic, no page can ever bounce the user back to
// where they already are. A sessionStorage hop-guard additionally stops stale
// cached copies of the pages from cycling.
(function(){
  var GUARD_KEY = 'bw_nav_loop_guard';

  function getLandingPage(uid){
    if(!uid) return 'Sign.html';
    if(localStorage.getItem('bw_doctorPremium_' + uid) === 'true'){
      // Preserve legacy normalization: doctor premium implies the doctor flag.
      if(localStorage.getItem('bw_accountType') !== 'doctor'){
        localStorage.setItem('bw_accountType', 'doctor');
      }
      return 'medical_dashboard_v2.html';
    }
    if(localStorage.getItem('bw_accountType') === 'doctor') return 'medical_dashboard_v2(2).html';
    if(localStorage.getItem('bw_premium_' + uid) === 'true') return 'bitewise-app(2).html';
    return 'bitewise-app.html';
  }

  function currentAuthPage(){
    // The premium app is served at "/" too, so treat the root path as that
    // page: a user already on "/" is never redirected away from it.
    if(window.location.pathname === '/' || window.location.pathname === ''){
      return 'bitewise-app(2).html';
    }
    return window.location.pathname.split('/').pop().split('?')[0] || 'index.html';
  }

  // Clears the hop-guard so one-shot, user-triggered redirects (login /
  // signup completion) are never blocked by an earlier page bounce.
  function clearRedirectGuard(){
    try { sessionStorage.removeItem(GUARD_KEY); } catch(e){}
  }

  function hopGuardAllows(target){
    var g = null;
    try { g = JSON.parse(sessionStorage.getItem(GUARD_KEY) || 'null'); } catch(e){}
    var now = Date.now();
    if(!g || !g.ts || now - g.ts > 10000){ g = {back: '', hops: 0, ts: now}; }
    if(g.back === target){
      sessionStorage.removeItem(GUARD_KEY);
      return false;
    }
    g.hops += 1;
    g.ts = now;
    if(g.hops > 3){
      sessionStorage.removeItem(GUARD_KEY);
      return false;
    }
    g.back = currentAuthPage();
    sessionStorage.setItem(GUARD_KEY, JSON.stringify(g));
    return true;
  }

  // Routes the current page to where this user belongs.
  // Returns true if a redirect was issued, false if the user stays.
  // Never redirects to the page the user is already on.
  function redirectToLanding(){
    var uid = localStorage.getItem('bw_uid') || '';
    var target = getLandingPage(uid);
    var here = currentAuthPage();
    if(target === here) return false;                 // requirement 7: same page -> do nothing
    if(!hopGuardAllows(target)) return false;         // stale-cache bounce protection
    console.log('[auth-routing] ' + here + ' -> ' + target +
      ' (bw_uid=' + uid + ', bw_accountType=' + localStorage.getItem('bw_accountType') +
      ', bw_premium=' + localStorage.getItem('bw_premium_' + uid) +
      ', bw_doctorPremium=' + localStorage.getItem('bw_doctorPremium_' + uid) + ')');
    window.location.replace(target);
    return true;
  }

  window.getLandingPage = getLandingPage;
  window.currentAuthPage = currentAuthPage;
  window.redirectToLanding = redirectToLanding;
  window.clearRedirectGuard = clearRedirectGuard;
})();
