# Tomcat 10.1 + Java 17 + WAR(설계 §2-3 WAS). OTel javaagent 와 OTEL_ 환경변수는 넣지 않는다(Q1).
verify_artifact petition.war
place_files was
ln -sf /usr/share/java/postgresql.jar /var/lib/tomcat10/lib/postgresql.jar
install -m 0644 -o tomcat -g tomcat "$BUNDLE/artifacts/petition.war" /var/lib/tomcat10/webapps/petition.war.new
mv -f /var/lib/tomcat10/webapps/petition.war.new /var/lib/tomcat10/webapps/petition.war
rm -rf /var/lib/tomcat10/webapps/petition
install -d -m 0755 /var/lib/petition-deploy
(cd "$BUNDLE/artifacts" && grep '  petition.war$' SHA256SUMS) > /var/lib/petition-deploy/war.sha256
restart_enabled tomcat10
# WAR 를 펼치고 앱이 설 때까지 다시 읽는다(settle, lib.sh). 상한은 종전 고리(2초 간격 60번)와 같은 120초.
settle 120 2 "앱 health 응답 없음" curl -fsS -m 3 -o /dev/null "http://127.0.0.1:${WAS_PORT}/${APP_CONTEXT}/api/health"
log "was ok: $(cat /var/lib/petition-deploy/war.sha256)"
