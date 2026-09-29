with open('bitewise-app.html', 'r', encoding='utf-8') as f:
    content = f.read()

# Build the functions to insert
functions_to_insert = '''

  function saveProfile(){
    var age = document.getElementById('editAge').value.trim();
    var weight = document.getElementById('editWeight').value.trim();
    var height = document.getElementById('editHeight').value.trim();
    var goalArr = getGroupChips('egGoal');
    var goal = goalArr.length ? goalArr[0] : '';
    var errEl = document.getElementById('profileError');
    errEl.style.display = 'none';
    if(!age || !weight || !height){
      errEl.textContent = 'Please fill in age, weight, and height.';
      errEl.style.display = 'block';
      return;
    }
    var a = Number(age), w = Number(weight), h = Number(height);
    if(!Number.isFinite(a) || a < 1 || a > 150 || !Number.isInteger(a)){
      errEl.textContent = 'Age must be a whole number between 1 and 150.';
      errEl.style.display = 'block';
      return;
    }
    if(!Number.isFinite(w) || w < 1 || w > 500){
      errEl.textContent = 'Weight must be a number between 1 and 500 kg.';
      errEl.style.display = 'block';
      return;
    }
    if(!Number.isFinite(h) || h < 1 || h > 300){
      errEl.textContent = 'Height must be a number between 1 and 300 cm.';
      errEl.style.display = 'block';
      return;
    }
    var conditions = getGroupChips('egConditions');
    var preferences = getGroupChips('egPreferences');
    var allergies = getGroupChips('egAllergies');
    var restrictions = getGroupChips('egRestrictions');
    var dislikes = getGroupChips('egDislikes');
    var profile = {age: a, weight: w, height: h, goal: goal, conditions: conditions, preferences: preferences, allergies: allergies, restrictions: restrictions, dislikes: dislikes, avatar: selectedAvatar};
    localStorage.setItem('bw_profile', JSON.stringify(profile));
    if(uid && window._bwProfileApi){
      window._bwProfileApi.goal = goal;
      window._bwProfileApi.conditions = conditions;
      window._bwProfileApi.preferences = preferences;
      window._bwProfileApi.allergies = allergies;
      window._bwProfileApi.restrictions = restrictions;
      window._bwProfileApi.dislikes = dislikes;
      window._bwProfileApi.avatar = selectedAvatar;
    }
    if(uid){
      api('PUT', '/auth/profile/'+uid, {age: a, weight: w, height: h, goal: goal, conditions: conditions, preferences: preferences, allergies: allergies, restrictions: restrictions, dislikes: dislikes}).catch(function(){});
    }
    loadProfile();
    go('profile');
  }

  function loadEditProfile(){
    var local = localStorage.getItem('bw_profile');
    var p = local ? JSON.parse(local) : {};
    var apiData = window._bwProfileApi || {};
    document.getElementById('editAge').value = p.age || '';
    document.getElementById('editWeight').value = p.weight || '';
    document.getElementById('editHeight').value = p.height || '';
    setChips('egGoal', p.goal ? [p.goal] : (apiData.goal ? [apiData.goal] : []));
    setChips('egConditions', p.conditions || apiData.conditions || []);
    setChips('egPreferences', p.preferences || apiData.preferences || []);
    setChips('egAllergies', p.allergies || apiData.allergies || []);
    setChips('egRestrictions', p.restrictions || apiData.restrictions || []);
    setChips('egDislikes', p.dislikes || apiData.dislikes || []);
    document.getElementById('profileError').style.display = 'none';
    // Initialize avatar selector
    selectedAvatar = p.avatar || 'avatar1';
    renderAvatarSelector();
    updateAvatarDisplay(selectedAvatar);
  }

  function getAvatars() {
    return [
      { id: 'avatar1', name: 'Alex', svg: '<svg viewBox="0 0 24 24" width="34" height="34" fill="none" stroke="var(--dark)" stroke-width="1.5"><circle cx="12" cy="8" r="4"/><path d="M4 20c0-4 4-6 8-6s8 2 8 6"/></svg>' },
      { id: 'avatar2', name: 'Sam', svg: '<svg viewBox="0 0 24 24" width="34" height="34" fill="none" stroke="var(--dark)" stroke-width="1.5"><circle cx="12" cy="8" r="4"/><path d="M4 20c0-4 4-6 8-6s8 2 8 6"/></svg><path d="M12 14v4" stroke="var(--sage)" stroke-width="2"/>' },
      { id: 'avatar3', name: 'Jordan', svg: '<svg viewBox="0 0 24 24" width="34" height="34" fill="none" stroke="var(--dark)" stroke-width="1.5"><circle cx="12" cy="7" r="4"/><path d="M4 21c0-3 3-5 8-5s8 2 8 5"/></svg>' },
      { id: 'avatar4', name: 'Taylor', svg: '<svg viewBox="0 0 24 24" width="34" height="34" fill="none" stroke="var(--dark)" stroke-width="1.5"><circle cx="12" cy="7" r="4"/><path d="M4 21c0-3 3-5 8-5s8 2 8 5"/></svg><path d="M8 10h8M8 14h4" stroke="var(--primary)" stroke-width="1.5" stroke-linecap="round"/>' },
      { id: 'avatar5', name: 'Casey', svg: '<svg viewBox="0 0 24 24" width="34" height="34" fill="none" stroke="var(--dark)" stroke-width="1.5"><circle cx="12" cy="8" r="4"/><path d="M4 20c0-4 4-6 8-6s8 2 8 6"/></svg><circle cx="9" cy="7" r="1" fill="var(--accent)" stroke="none"/><circle cx="15" cy="7" r="1" fill="var(--accent)" stroke="none"/>' },
      { id: 'avatar6', name: 'Riley', svg: '<svg viewBox="0 0 24 24" width="34" height="34" fill="none" stroke="var(--dark)" stroke-width="1.5"><circle cx="12" cy="8" r="4"/><path d="M4 20c0-4 4-6 8-6s8 2 8 6"/></svg><path d="M10 10h4M10 13h4" stroke="var(--sage)" stroke-width="1.5" stroke-linecap="round"/>' },
      { id: 'avatar7', name: 'Morgan', svg: '<svg viewBox="0 0 24 24" width="34" height="34" fill="none" stroke="var(--dark)" stroke-width="1.5"><circle cx="12" cy="7" r="4"/><path d="M4 21c0-3 3-5 8-5s8 2 8 5"/></svg><ellipse cx="12" cy="11" rx="3" ry="1.5" stroke="var(--primary)" stroke-width="1.5" fill="none"/>' },
      { id: 'avatar8', name: 'Drew', svg: '<svg viewBox="0 0 24 24" width="34" height="34" fill="none" stroke="var(--dark)" stroke-width="1.5"><circle cx="12" cy="8" r="4"/><path d="M4 20c0-4 4-6 8-6s8 2 8 6"/></svg><path d="M9 10l1 3 3-3" stroke="var(--sage)" stroke-width="2" fill="none"/>' },
      { id: 'avatar9', name: 'Quinn', svg: '<svg viewBox="0 0 24 24" width="34" height="34" fill="none" stroke="var(--dark)" stroke-width="1.5"><circle cx="12" cy="8" r="4"/><path d="M4 20c0-4 4-6 8-6s8 2 8 6"/></svg><path d="M9 11l1 2 3-4" stroke="var(--accent)" stroke-width="2" fill="none"/>' },
      { id: 'avatar10', name: 'Avery', svg: '<svg viewBox="0 0 24 24" width="34" height="34" fill="none" stroke="var(--dark)" stroke-width="1.5"><circle cx="12" cy="7" r="4"/><path d="M4 21c0-3 3-5 8-5s8 2 8 5"/></svg><circle cx="12" cy="11" r="2" stroke="var(--primary)" stroke-width="1.5" fill="none"/>' }
    ];
  }
 
  function renderAvatarSelector() {
    var container = document.getElementById('avatarSelector');
    if (!container) return;
    var avatars = getAvatars();
    var currentAvatar = getCurrentAvatar();
    container.innerHTML = avatars.map(function(a) {
      return '<button type="button" class="avatar-option' + (a.id === currentAvatar ? ' selected' : '') + '" ' +
             'data-avatar-id="' + a.id + '" ' +
             'onclick="selectAvatar(\\'' + a.id + '\\')" ' +
             'style="width:60px;height:60px;border-radius:50%;border:3px solid transparent;background:var(--surface);display:flex;align-items:center;justify-content:center;cursor:pointer;transition:all 0.2s;box-shadow:0 2px 8px rgba(0,0,0,0.08);" ' +
             'onmouseover="this.style.transform=\'scale(1.05)\'" ' +
             'onmouseout="this.style.transform=\'scale(1)\'">' +
             a.svg + '</button>';
    }).join('');
  }
 
  function getCurrentAvatar() {
    var local = localStorage.getItem('bw_profile');
    var p = local ? JSON.parse(local) : {};
    return p.avatar || 'avatar1';
  }
 
  function selectAvatar(avatarId) {
    selectedAvatar = avatarId;
    var preview = document.getElementById('editAvatarPreview');
    var avatar = getAvatars().find(function(a) { return a.id === avatarId; });
    if (preview && avatar) preview.innerHTML = avatar.svg;
    document.querySelectorAll('.avatar-option').forEach(function(opt) {
      opt.classList.toggle('selected', opt.dataset.avatarId === avatarId);
    });
  }

var selectedAvatar = 'avatar1';

'''

# Insert before getGroupChips
insert_pos = content.find('function getGroupChips')
new_content = content[:insert_pos] + functions_to_insert + content[insert_pos:]

with open('bitewise-app.html', 'w', encoding='utf-8') as f:
    f.write(new_content)

print('Inserted functions. New length:', len(new_content))