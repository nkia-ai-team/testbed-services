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

RUN apt-get update && apt-get install -y maven && \
    mvn -pl shop-common -am install -DskipTests -q && \
    mvn -pl restaurant-service dependency:go-offline -q

COPY restaurant-service/src ./restaurant-service/src
RUN mvn -pl restaurant-service package -DskipTests -q

FROM eclipse-temurin:17-jre
WORKDIR /app
COPY --from=builder /build/restaurant-service/target/*.jar app.jar
RUN mkdir -p /opt/apm
EXPOSE 8081
ENTRYPOINT ["java", "-jar", "app.jar"]
