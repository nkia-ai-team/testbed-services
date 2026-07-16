FROM eclipse-temurin:17-jdk AS builder
ENV JAVA_TOOL_OPTIONS=-Djava.net.preferIPv4Stack=true
RUN echo 'precedence ::ffff:0:0/96 100' >> /etc/gai.conf
WORKDIR /build

COPY pom.xml ./pom.xml
COPY shop-common ./shop-common
COPY order-service/pom.xml ./order-service/pom.xml
COPY restaurant-service/pom.xml ./restaurant-service/pom.xml
COPY dispatch-service/pom.xml ./dispatch-service/pom.xml
COPY payment-service/pom.xml ./payment-service/pom.xml
COPY notify-service/pom.xml ./notify-service/pom.xml

RUN apt-get -o Acquire::ForceIPv4=true update && apt-get -o Acquire::ForceIPv4=true install -y maven && \
    mvn -pl shop-common -am install -DskipTests -q && \
    mvn -pl notify-service dependency:go-offline -q

COPY notify-service/src ./notify-service/src
RUN mvn -pl notify-service package -DskipTests -q

FROM eclipse-temurin:17-jre
WORKDIR /app
COPY --from=builder /build/notify-service/target/*.jar app.jar
RUN mkdir -p /opt/apm
EXPOSE 8084
ENTRYPOINT ["java", "-jar", "app.jar"]
