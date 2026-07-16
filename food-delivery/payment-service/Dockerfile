FROM eclipse-temurin:17-jdk AS builder
ENV JAVA_TOOL_OPTIONS=-Djava.net.preferIPv4Stack=true
RUN echo 'precedence ::ffff:0:0/96 100' >> /etc/gai.conf
WORKDIR /build

COPY pom.xml ./pom.xml
COPY mvnw ./mvnw
COPY .mvn ./.mvn
COPY shop-common ./shop-common
COPY order-service/pom.xml ./order-service/pom.xml
COPY restaurant-service/pom.xml ./restaurant-service/pom.xml
COPY dispatch-service/pom.xml ./dispatch-service/pom.xml
COPY payment-service/pom.xml ./payment-service/pom.xml
COPY notify-service/pom.xml ./notify-service/pom.xml

# apt로 maven을 설치하지 않는다 — 사무실망에서 ubuntu 미러 연결이 간헐 실패
# (2026-07-16 실측: IPv6/IPv4 모두 타임아웃). Maven Wrapper는 Maven Central에서
# 배포판을 받아 저장소 의존을 의존성 다운로드와 같은 경로 하나로 줄인다.
RUN ./mvnw -pl shop-common -am install -DskipTests -q && \
    ./mvnw -pl payment-service dependency:go-offline -q

COPY payment-service/src ./payment-service/src
RUN ./mvnw -pl payment-service package -DskipTests -q

FROM eclipse-temurin:17-jre
WORKDIR /app
COPY --from=builder /build/payment-service/target/*.jar app.jar
RUN mkdir -p /opt/apm
EXPOSE 8083
ENTRYPOINT ["java", "-jar", "app.jar"]
