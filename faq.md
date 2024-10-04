# libpyvex.so -> not found, but provided by a python dependency
When you're installing a wheel, python doesn't provide 
'built time' dependencies (because in the python world, wheels
are ready made binaries, while for nix they need to have their 
LD paths patched). 

You need to add final.{pkg providing the .so} to the buildInputs, 
and something like ''addAutoPatchelfSearchPath "${final.pkg}/${final.python.sitePackages}/${final.pkg.pname}/lib"'' to the preFixup phase.
