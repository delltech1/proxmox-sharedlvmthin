/* The locale setting never contains credentials or authentication state. */
(() => {
  'use strict';
  const de={
    'Proxmox VE Authentication':'Proxmox VE Authentifizierung',
    'Use an existing PVE administrator account.':'Verwenden Sie ein vorhandenes PVE-Administratorkonto.',
    'Username / realm':'Benutzername / Realm','Password':'Passwort','Sign in':'Anmelden',
    'Two-Factor Authentication':'Zwei-Faktor-Authentifizierung','Factor':'Faktor','Response':'Antwort',
    'Recovery key':'Wiederherstellungsschlüssel','Verify':'Bestätigen','READ ONLY':'SCHREIBGESCHÜTZT',
    'Username and password required.':'Benutzername und Passwort sind erforderlich.',
    'Authentication failed.':'Authentifizierung fehlgeschlagen.',
    'Authenticated, but PVE administrator privileges are required.':'Authentifiziert, aber PVE-Administratorrechte sind erforderlich.',
    'Too many attempts. Try again shortly.':'Zu viele Versuche. Bitte versuchen Sie es später erneut.',
    'Two-factor challenge expired. Please sign in again.':'Die Zwei-Faktor-Anfrage ist abgelaufen. Bitte melden Sie sich erneut an.',
    'Too many two-factor attempts. Please sign in again.':'Zu viele Zwei-Faktor-Versuche. Bitte melden Sie sich erneut an.',
    'Invalid factor response.':'Ungültige Faktor-Antwort.',
    'Two-factor verification failed.':'Zwei-Faktor-Prüfung fehlgeschlagen.',
    'The response is verified by Proxmox VE. WebAuthn needs a browser-origin ceremony and is not emulated in this version; use another configured PVE factor.':'Die Antwort wird von Proxmox VE geprüft. WebAuthn erfordert einen browserbasierten Vorgang und wird in dieser Version nicht nachgebildet; verwenden Sie einen anderen eingerichteten PVE-Faktor.'
  };
  const select=document.getElementById('login-language');if(!select)return;
  const originals=new WeakMap();
  const elements=[...document.querySelectorAll('.card h1,.card .sub,.card label,.card button,.card option,.card .err,.card .note,.ro')];
  for(const element of elements)originals.set(element,element.textContent);
  const footer=document.querySelector('.w > .note:last-child');
  const footerText=footer&&footer.firstChild; const footerOriginal=footerText&&footerText.nodeValue;
  function apply(lang){
    document.documentElement.lang=lang;
    for(const element of elements){
      const old=originals.get(element);let next=lang==='de'?(de[old]||old):old;
      if(lang==='de' && old.startsWith('Additional PVE factor required for ') && old.endsWith('.'))next='Zusätzlicher PVE-Faktor erforderlich für '+old.slice(35);
      element.textContent=next;
    }
    if(footerText)footerText.nodeValue=lang==='de'?'Authentifizierung durch Proxmox VE · ':footerOriginal;
  }
  let lang='en';try{lang=localStorage.getItem('bastrix-language')==='de'?'de':'en'}catch{}
  select.value=lang;apply(lang);
  select.addEventListener('change',()=>{lang=select.value==='de'?'de':'en';try{localStorage.setItem('bastrix-language',lang)}catch{};apply(lang)});
})();
