FROM mcr.microsoft.com/dotnet/aspnet:9.0
WORKDIR /app
COPY .meshagent/publish/ .
EXPOSE 5000
ENTRYPOINT ["dotnet", "MeshAgentHello.dll"]
