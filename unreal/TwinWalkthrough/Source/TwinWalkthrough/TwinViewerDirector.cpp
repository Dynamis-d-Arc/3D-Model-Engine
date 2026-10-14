#include "TwinViewerDirector.h"

#include "Async/Async.h"
#include "Components/BrushComponent.h"
#include "Components/DirectionalLightComponent.h"
#include "Components/PointLightComponent.h"
#include "Dom/JsonObject.h"
#include "Engine/CollisionProfile.h"
#include "Engine/DirectionalLight.h"
#include "Engine/PointLight.h"
#include "Engine/Texture2D.h"
#include "Engine/World.h"
#include "EngineUtils.h"
#include "GenericPlatform/GenericPlatformHttp.h"
#include "HAL/FileManager.h"
#include "HttpModule.h"
#include "IImageWrapperModule.h"
#include "Interfaces/IHttpResponse.h"
#include "Materials/Material.h"
#include "Materials/MaterialInstanceDynamic.h"
#include "Misc/Base64.h"
#include "Misc/CommandLine.h"
#include "Misc/FileHelper.h"
#include "Misc/Parse.h"
#include "Misc/Paths.h"
#include "Modules/ModuleManager.h"
#include "NavMesh/NavMeshBoundsVolume.h"
#include "NavigationData.h"
#include "NavigationSystem.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"
#include "TextureResource.h"
#include "TwinAirflowActor.h"
#include "TwinRuntimeMeshActor.h"

DEFINE_LOG_CATEGORY_STATIC(LogTwinViewer, Log, All);

namespace
{
	const TCHAR* SurfacePath = TEXT("/Game/Viewer/Materials/M_TwinSurface.M_TwinSurface");
	const TCHAR* GlassPath = TEXT("/Game/Viewer/Materials/M_TwinGlass.M_TwinGlass");
	constexpr double TolCm = 1.0;

	TSharedPtr<FJsonObject> ParseJson(const FString& Text)
	{
		TSharedPtr<FJsonObject> J;
		const TSharedRef<TJsonReader<>> R = TJsonReaderFactory<>::Create(Text);
		return FJsonSerializer::Deserialize(R, J) ? J : nullptr;
	}

	FString Str(const TSharedPtr<FJsonObject>& O, const TCHAR* Key)
	{
		FString S;
		if (O.IsValid() && O->TryGetStringField(Key, S))
		{
			return S;
		}
		double N;
		if (O.IsValid() && O->TryGetNumberField(Key, N))
		{
			return FString::SanitizeFloat(N);
		}
		return FString();
	}

	double NumOr(const TSharedPtr<FJsonObject>& O, const TCHAR* Key, double Default)
	{
		double V = Default;
		return (O.IsValid() && O->TryGetNumberField(Key, V)) ? V : Default;
	}

	bool Vec(const TSharedPtr<FJsonObject>& O, const TCHAR* Key, FVector& Out)
	{
		const TArray<TSharedPtr<FJsonValue>>* A = nullptr;
		if (!O.IsValid() || !O->TryGetArrayField(Key, A) || A->Num() < 3)
		{
			return false;
		}
		Out = FVector((*A)[0]->AsNumber(), (*A)[1]->AsNumber(), (*A)[2]->AsNumber());
		return true;
	}

	FBox BoxField(const TSharedPtr<FJsonObject>& O, const TCHAR* Key)
	{
		const TArray<TSharedPtr<FJsonValue>>* A = nullptr;
		if (!O.IsValid() || !O->TryGetArrayField(Key, A) || A->Num() != 2)
		{
			return FBox(ForceInit);
		}
		const TArray<TSharedPtr<FJsonValue>>& Lo = (*A)[0]->AsArray();
		const TArray<TSharedPtr<FJsonValue>>& Hi = (*A)[1]->AsArray();
		if (Lo.Num() != 3 || Hi.Num() != 3)
		{
			return FBox(ForceInit);
		}
		return FBox(FVector(Lo[0]->AsNumber(), Lo[1]->AsNumber(), Lo[2]->AsNumber()),
		            FVector(Hi[0]->AsNumber(), Hi[1]->AsNumber(), Hi[2]->AsNumber()));
	}

	double BoxDeviation(const FBox& A, const FBox& B)
	{
		return FMath::Max(FMath::Max(FMath::Max(FMath::Abs(A.Min.X - B.Min.X), FMath::Abs(A.Min.Y - B.Min.Y)),
		                             FMath::Max(FMath::Abs(A.Min.Z - B.Min.Z), FMath::Abs(A.Max.X - B.Max.X))),
		                  FMath::Max(FMath::Abs(A.Max.Y - B.Max.Y), FMath::Abs(A.Max.Z - B.Max.Z)));
	}

	TArray<TSharedPtr<FJsonValue>> BoxJson(const FBox& B)
	{
		auto V = [](const FVector& P) -> TSharedPtr<FJsonValue>
		{
			return MakeShared<FJsonValueArray>(TArray<TSharedPtr<FJsonValue>>{
				MakeShared<FJsonValueNumber>(P.X), MakeShared<FJsonValueNumber>(P.Y), MakeShared<FJsonValueNumber>(P.Z)});
		};
		return {V(B.Min), V(B.Max)};
	}

	FColor HexColor(const FString& Hex)
	{
		return Hex.Len() >= 7 ? FColor::FromHex(Hex) : FColor::White;
	}
}

ATwinViewerDirector::ATwinViewerDirector()
{
	PrimaryActorTick.bCanEverTick = true;
	PrimaryActorTick.bTickEvenWhenPaused = true;
}

ATwinViewerDirector* ATwinViewerDirector::Get(const UWorld* World)
{
	if (!World)
	{
		return nullptr;
	}
	for (TActorIterator<ATwinViewerDirector> It(const_cast<UWorld*>(World)); It; ++It)
	{
		return *It;
	}
	return nullptr;
}

// ------------------------------------------------------------------------------------------------
// launch information
// ------------------------------------------------------------------------------------------------

bool ATwinViewerDirector::ParseLaunch(const FString& InText, FTwinLaunch& Out, FString& Error)
{
	FString Text = InText.TrimStartAndEnd();
	if (Text.StartsWith(TEXT("twinview:")))
	{
		FString Decoded;
		if (!FBase64::Decode(Text.RightChop(9).Replace(TEXT("-"), TEXT("+")).Replace(TEXT("_"), TEXT("/")), Decoded))
		{
			Error = TEXT("the launch code is not valid base64");
			return false;
		}
		Text = Decoded;
	}
	const TSharedPtr<FJsonObject> J = ParseJson(Text);
	if (!J.IsValid() || Str(J, TEXT("format")) != TEXT("twin-viewer-launch"))
	{
		Error = TEXT("this is not a twin viewer launch file (format twin-viewer-launch)");
		return false;
	}
	Out.Server = Str(J, TEXT("server"));
	Out.Server.RemoveFromEnd(TEXT("/"));
	Out.ProjectId = Str(J, TEXT("project_id"));
	Out.BuildId = Str(J, TEXT("build_id"));
	Out.Token = Str(J, TEXT("token"));
	Out.ProjectName = Str(J, TEXT("project_name"));
	Out.ExpiresAt = NumOr(J, TEXT("expires_at"), 0.0);
	if (!Out.IsComplete())
	{
		Error = TEXT("the launch file lacks server, project_id, build_id or token");
		return false;
	}
	if (!Out.Server.StartsWith(TEXT("http://")) && !Out.Server.StartsWith(TEXT("https://")))
	{
		Error = FString::Printf(TEXT("the server address must start with http:// or https:// (got '%s')"), *Out.Server);
		return false;
	}
	return true;
}

bool ATwinViewerDirector::LaunchFromCommandLine(FTwinLaunch& Out, FString& Error)
{
	const TCHAR* Cmd = FCommandLine::Get();
	FString File;
	if (FParse::Value(Cmd, TEXT("TwinLaunch="), File))
	{
		FString Text;
		if (!FFileHelper::LoadFileToString(Text, *File))
		{
			Error = FString::Printf(TEXT("cannot read the launch file %s"), *File);
			return false;
		}
		return ParseLaunch(Text, Out, Error);
	}
	FParse::Value(Cmd, TEXT("TwinServer="), Out.Server);
	FParse::Value(Cmd, TEXT("TwinProject="), Out.ProjectId);
	FParse::Value(Cmd, TEXT("TwinBuild="), Out.BuildId);
	FParse::Value(Cmd, TEXT("TwinToken="), Out.Token);
	FString TokenFile;
	if (Out.Token.IsEmpty() && FParse::Value(Cmd, TEXT("TwinTokenFile="), TokenFile))
	{
		FFileHelper::LoadFileToString(Out.Token, *TokenFile);
		Out.Token.TrimStartAndEndInline();
	}
	Out.Server.RemoveFromEnd(TEXT("/"));
	if (Out.Server.IsEmpty() && Out.ProjectId.IsEmpty() && Out.Token.IsEmpty())
	{
		return false;  // nothing given: the viewer waits for a pasted launch code
	}
	if (!Out.IsComplete())
	{
		Error = TEXT("-TwinServer, -TwinProject, -TwinBuild and -TwinToken (or -TwinTokenFile) are all required");
		return false;
	}
	return true;
}

// ------------------------------------------------------------------------------------------------
// lifecycle
// ------------------------------------------------------------------------------------------------

void ATwinViewerDirector::BeginPlay()
{
	Super::BeginPlay();
	ImageWrappers = &FModuleManager::LoadModuleChecked<IImageWrapperModule>(TEXT("ImageWrapper"));
	SurfaceBase = LoadObject<UMaterialInterface>(nullptr, SurfacePath);
	GlassBase = LoadObject<UMaterialInterface>(nullptr, GlassPath);
	if (!SurfaceBase)
	{
		UE_LOG(LogTwinViewer, Warning, TEXT("viewer material %s missing; using the engine default material"), SurfacePath);
		SurfaceBase = UMaterial::GetDefaultMaterial(MD_Surface);
	}
	if (!GlassBase)
	{
		GlassBase = SurfaceBase;
	}
	DefaultMaterial = UMaterialInstanceDynamic::Create(SurfaceBase, this);
	DefaultMaterial->SetVectorParameterValue(TEXT("BaseColor"), FLinearColor(0.6f, 0.6f, 0.6f));
	bNoCache = FParse::Param(FCommandLine::Get(), TEXT("TwinNoCache"));
	int32 MaxMb = 0;
	if (FParse::Value(FCommandLine::Get(), TEXT("TwinMaxModelMB="), MaxMb) && MaxMb > 0)
	{
		MaxModelBytes = (int64)MaxMb * 1024 * 1024;
	}

	FTwinLaunch L;
	FString Err;
	if (LaunchFromCommandLine(L, Err))
	{
		Open(L);
	}
	else if (!Err.IsEmpty())
	{
		Fail(TEXT("LAUNCH_INVALID"), Err);
	}
}

void ATwinViewerDirector::EndPlay(const EEndPlayReason::Type Reason)
{
	++RequestSerial;  // late HTTP / worker callbacks become no-ops
	Super::EndPlay(Reason);
}

void ATwinViewerDirector::Fail(const FString& Code, const FString& Message)
{
	State = ETwinLoadState::Error;
	ErrorCode = Code;
	ErrorMessage = Message;
	Stage = TEXT("Failed");
	UE_LOG(LogTwinViewer, Error, TEXT("[twin] load failed: %s %s"), *Code, *Message);
}

void ATwinViewerDirector::Open(const FTwinLaunch& InLaunch)
{
	Unload();
	Launch = InLaunch;
	ErrorCode.Reset();
	ErrorMessage.Reset();
	Timings.Reset();
	LoadStart = StageStart = FPlatformTime::Seconds();
	if (Launch.ExpiresAt > 0 && Launch.ExpiresAt < (double)FDateTime::UtcNow().ToUnixTimestamp())
	{
		Fail(TEXT("TOKEN_EXPIRED"), TEXT("This viewer link has expired. Open the building again from the web app to get a new one."));
		return;
	}
	UE_LOG(LogTwinViewer, Display, TEXT("[twin] opening %s / %s from %s"), *Launch.ProjectId, *Launch.BuildId, *Launch.Server);
	FetchManifest();
	if (Buildings.Num() == 0)
	{
		RefreshBuildings();
	}
}

void ATwinViewerDirector::OpenBuild(const FString& ProjectId, const FString& BuildId)
{
	FTwinLaunch L = Launch;
	L.ProjectId = ProjectId;
	L.BuildId = BuildId;
	for (const FTwinBuildingEntry& B : Buildings)
	{
		if (B.ProjectId == ProjectId)
		{
			L.ProjectName = B.Name;
		}
	}
	Open(L);
}

void ATwinViewerDirector::Retry()
{
	if (Launch.IsComplete())
	{
		Open(Launch);
	}
}

void ATwinViewerDirector::Unload()
{
	++RequestSerial;
	if (AirflowActor)
	{
		AirflowActor->Clear();
	}
	for (AActor* A : NodeActors)
	{
		if (IsValid(A))
		{
			A->Destroy();
		}
	}
	for (AActor* A : Spawned)
	{
		if (IsValid(A))
		{
			A->Destroy();
		}
	}
	const bool bHadBuilding = NodeActors.Num() > 0;
	NodeActors.Reset();
	ExpectedBox.Reset();
	NodeNames.Reset();
	Spawned.Reset();
	Textures.Reset();
	Materials.Reset();
	Model.Reset();
	Manifest.Reset();
	MetaByName.Reset();
	RoomsById.Reset();
	Telemetry = FTwinTelemetry();
	Airflow.Reset();
	AirflowError.Reset();
	Validation.Reset();
	MaxDeviationCm = 0.0;
	ValidationChecked = ValidationFailures = ActorsSpawned = 0;
	TrianglesSpawned = 0;
	BytesDone = BytesTotal = 0;
	bFromCache = false;
	bPollInFlight = false;
	Progress = 0.f;
	State = ETwinLoadState::Idle;
	if (bHadBuilding)
	{
		OnUnloaded.Broadcast();
	}
}

// ------------------------------------------------------------------------------------------------
// HTTP
// ------------------------------------------------------------------------------------------------

FString ATwinViewerDirector::BuildPath(const FString& Leaf) const
{
	return FString::Printf(TEXT("/api/viewer/v1/buildings/%s/builds/%s/%s"), *FGenericPlatformHttp::UrlEncode(Launch.ProjectId),
	                       *FGenericPlatformHttp::UrlEncode(Launch.BuildId), *Leaf);
}

TSharedRef<IHttpRequest, ESPMode::ThreadSafe> ATwinViewerDirector::NewRequest(const FString& Path, float TimeoutS) const
{
	TSharedRef<IHttpRequest, ESPMode::ThreadSafe> R = FHttpModule::Get().CreateRequest();
	R->SetURL(Launch.Server + Path);
	R->SetVerb(TEXT("GET"));
	R->SetHeader(TEXT("Authorization"), TEXT("Bearer ") + Launch.Token);
	R->SetHeader(TEXT("User-Agent"), TEXT("TwinViewer/1 (Unreal Engine)"));
	R->SetTimeout(TimeoutS);
	return R;
}

bool ATwinViewerDirector::ErrorFromResponse(FHttpResponsePtr Response, bool bOk, const FString& Server, FString& Code, FString& Message)
{
	if (!bOk || !Response.IsValid() || Response->GetResponseCode() <= 0)
	{
		Code = TEXT("SERVER_UNREACHABLE");
		Message = FString::Printf(TEXT("Cannot reach the backend at %s (%s). Check that it is running and reachable from this computer."),
		                          *Server, Response.IsValid() ? LexToString(Response->GetFailureReason()) : TEXT("no response"));
		return true;
	}
	const int32 Status = Response->GetResponseCode();
	if (Status < 400)
	{
		return false;
	}
	Code = FString::Printf(TEXT("HTTP_%d"), Status);
	Message = FString::Printf(TEXT("The backend answered HTTP %d."), Status);
	const TSharedPtr<FJsonObject> J = ParseJson(Response->GetContentAsString());
	const TSharedPtr<FJsonObject>* E = nullptr;
	if (J.IsValid() && J->TryGetObjectField(TEXT("error"), E))
	{
		Code = Str(*E, TEXT("code"));
		Message = Str(*E, TEXT("message"));
	}
	return true;
}

void ATwinViewerDirector::GetJson(const FString& Path, FJsonDone OnOk, FFail OnFail, float TimeoutS)
{
	TSharedRef<IHttpRequest, ESPMode::ThreadSafe> R = NewRequest(Path, TimeoutS);
	R->SetHeader(TEXT("Accept"), TEXT("application/json"));
	R->SetHeader(TEXT("Connection"), TEXT("close"));  // small periodic requests; servers drop idle keep-alives
	const FString Server = Launch.Server;
	R->OnProcessRequestComplete().BindWeakLambda(this, [Server, OnOk, OnFail](FHttpRequestPtr, FHttpResponsePtr Resp, bool bOk)
	{
		FString Code, Message;
		if (ErrorFromResponse(Resp, bOk, Server, Code, Message))
		{
			OnFail(Code, Message);
			return;
		}
		const TSharedPtr<FJsonObject> J = ParseJson(Resp->GetContentAsString());
		if (!J.IsValid())
		{
			OnFail(TEXT("BAD_RESPONSE"), TEXT("The backend's answer is not valid JSON."));
			return;
		}
		OnOk(J);
	});
	R->ProcessRequest();
}

void ATwinViewerDirector::RefreshBuildings()
{
	if (!Launch.IsComplete() || bBuildingsLoading)
	{
		return;
	}
	bBuildingsLoading = true;
	BuildingsError.Reset();
	GetJson(TEXT("/api/viewer/v1/buildings"), [this](TSharedPtr<FJsonObject> J)
	{
		bBuildingsLoading = false;
		Buildings.Reset();
		for (const TSharedPtr<FJsonValue>& P : J->GetArrayField(TEXT("buildings")))
		{
			const TSharedPtr<FJsonObject> PO = P->AsObject();
			for (const TSharedPtr<FJsonValue>& B : PO->GetArrayField(TEXT("builds")))
			{
				const TSharedPtr<FJsonObject> BO = B->AsObject();
				FTwinBuildingEntry& E = Buildings.AddDefaulted_GetRef();
				E.ProjectId = Str(PO, TEXT("project_id"));
				E.Name = Str(PO, TEXT("name"));
				E.BuildId = Str(BO, TEXT("build_id"));
				E.Version = (int32)NumOr(BO, TEXT("version"), 0);
				E.Bytes = (int64)NumOr(BO, TEXT("model_bytes"), 0);
				E.Rooms = (int32)NumOr(BO, TEXT("rooms"), 0);
				E.Furniture = (int32)NumOr(BO, TEXT("furniture"), 0);
			}
		}
	}, [this](const FString& Code, const FString& Message)
	{
		bBuildingsLoading = false;
		BuildingsError = FString::Printf(TEXT("%s: %s"), *Code, *Message);
	});
}

// ------------------------------------------------------------------------------------------------
// load pipeline
// ------------------------------------------------------------------------------------------------

void ATwinViewerDirector::FetchManifest()
{
	State = ETwinLoadState::FetchingManifest;
	Stage = TEXT("Fetching building information");
	Progress = 0.02f;
	const int32 Serial = RequestSerial;
	GetJson(BuildPath(TEXT("manifest")), [this, Serial](TSharedPtr<FJsonObject> J)
	{
		if (Serial != RequestSerial)
		{
			return;
		}
		if (Str(J, TEXT("format")) != TEXT("twin-viewer-manifest") || NumOr(J, TEXT("manifest_version"), 0) != 1)
		{
			Fail(TEXT("MANIFEST_UNSUPPORTED"), TEXT("The backend sent a manifest this viewer version does not understand; update the viewer."));
			return;
		}
		Manifest = J;
		for (const TCHAR* Key : {TEXT("objects"), TEXT("furniture")})
		{
			for (const TSharedPtr<FJsonValue>& V : J->GetArrayField(Key))
			{
				const TSharedPtr<FJsonObject> O = V->AsObject();
				MetaByName.Add(Str(O, TEXT("name")), O);
			}
		}
		for (const TSharedPtr<FJsonValue>& V : J->GetArrayField(TEXT("rooms")))
		{
			RoomsById.Add(Str(V->AsObject(), TEXT("id")), V->AsObject());
		}
		Launch.ProjectName = Str(J->GetObjectField(TEXT("project")), TEXT("name"));
		BytesTotal = (int64)NumOr(J->GetObjectField(TEXT("model")), TEXT("bytes"), 0);
		if (BytesTotal > MaxModelBytes)
		{
			Fail(TEXT("MODEL_TOO_LARGE"), FString::Printf(TEXT("The model is %.0f MB; this viewer is limited to %.0f MB (-TwinMaxModelMB=)."),
			                                              BytesTotal / 1048576.0, MaxModelBytes / 1048576.0));
			return;
		}
		Timings.Add(TEXT("manifest_s"), FPlatformTime::Seconds() - StageStart);
		FetchModel();
	}, [this, Serial](const FString& Code, const FString& Message)
	{
		if (Serial == RequestSerial)
		{
			Fail(Code, Message);
		}
	});
}

FString ATwinViewerDirector::CachePath() const
{
	const FString Sha = Str(Manifest->GetObjectField(TEXT("model")), TEXT("sha256"));
	return FPaths::ProjectSavedDir() / TEXT("TwinCache") / Launch.ProjectId / Launch.BuildId / (Sha.Left(32) + TEXT(".glb"));
}

void ATwinViewerDirector::FetchModel()
{
	StageStart = FPlatformTime::Seconds();
	const FString Cache = CachePath();
	const int32 Serial = RequestSerial;
	if (!bNoCache && IFileManager::Get().FileSize(*Cache) == BytesTotal && BytesTotal > 0)
	{
		State = ETwinLoadState::Parsing;
		Stage = TEXT("Reading the cached model");
		bFromCache = true;
		TWeakObjectPtr<ATwinViewerDirector> Weak(this);
		Async(EAsyncExecution::ThreadPool, [Weak, Serial, Cache]()
		{
			TArray<uint8> Bytes;
			const bool bRead = FFileHelper::LoadFileToArray(Bytes, *Cache);
			AsyncTask(ENamedThreads::GameThread, [Weak, Serial, bRead, Bytes = MoveTemp(Bytes)]() mutable
			{
				if (!Weak.IsValid() || Serial != Weak->RequestSerial)
				{
					return;
				}
				if (!bRead)
				{
					Weak->Fail(TEXT("CACHE_UNREADABLE"), TEXT("The cached model could not be read; run with -TwinNoCache."));
					return;
				}
				Weak->BytesDone = Bytes.Num();
				Weak->ParseModel(MoveTemp(Bytes));
			});
		});
		return;
	}

	State = ETwinLoadState::Downloading;
	Stage = TEXT("Downloading the 3D model");
	TSharedRef<IHttpRequest, ESPMode::ThreadSafe> R = NewRequest(BuildPath(TEXT("model.glb")),
	                                                              FMath::Max(120.f, (float)(BytesTotal / (64.0 * 1024.0))));
	R->SetActivityTimeout(60.f);
	R->OnRequestProgress64().BindWeakLambda(this, [this, Serial](FHttpRequestPtr, uint64, uint64 Received)
	{
		if (Serial == RequestSerial)
		{
			BytesDone = (int64)Received;
			Progress = 0.05f + 0.45f * (BytesTotal > 0 ? (float)((double)BytesDone / BytesTotal) : 0.f);
		}
	});
	const FString Server = Launch.Server;
	R->OnProcessRequestComplete().BindWeakLambda(this, [this, Serial, Server, Cache](FHttpRequestPtr, FHttpResponsePtr Resp, bool bOk)
	{
		if (Serial != RequestSerial)
		{
			return;
		}
		FString Code, Message;
		if (ErrorFromResponse(Resp, bOk, Server, Code, Message))
		{
			Fail(Code, Message);
			return;
		}
		TArray<uint8> Bytes = Resp->GetContent();
		if (Bytes.Num() != BytesTotal)
		{
			Fail(TEXT("MODEL_TRUNCATED"), FString::Printf(TEXT("Downloaded %d bytes, expected %lld."), Bytes.Num(), BytesTotal));
			return;
		}
		Timings.Add(TEXT("download_s"), FPlatformTime::Seconds() - StageStart);
		BytesDone = Bytes.Num();
		if (!bNoCache)
		{
			IFileManager::Get().MakeDirectory(*FPaths::GetPath(Cache), true);
			const FString Tmp = Cache + TEXT(".part");
			if (FFileHelper::SaveArrayToFile(Bytes, *Tmp))
			{
				IFileManager::Get().Move(*Cache, *Tmp, true);
			}
		}
		ParseModel(MoveTemp(Bytes));
	});
	R->ProcessRequest();
}

void ATwinViewerDirector::ParseModel(TArray<uint8>&& Bytes)
{
	State = ETwinLoadState::Parsing;
	Stage = TEXT("Reading the model (meshes, materials, textures)");
	Progress = 0.52f;
	StageStart = FPlatformTime::Seconds();
	const int32 Serial = RequestSerial;
	TWeakObjectPtr<ATwinViewerDirector> Weak(this);
	IImageWrapperModule* Wrappers = ImageWrappers;
	Async(EAsyncExecution::ThreadPool, [Weak, Serial, Wrappers, Bytes = MoveTemp(Bytes)]()
	{
		TSharedPtr<TwinGltf::FModel> M = MakeShared<TwinGltf::FModel>();
		FString Err;
		const bool bOk = TwinGltf::Parse(Bytes, Wrappers, *M, Err);
		AsyncTask(ENamedThreads::GameThread, [Weak, Serial, bOk, M, Err]()
		{
			if (!Weak.IsValid() || Serial != Weak->RequestSerial)
			{
				return;
			}
			if (!bOk)
			{
				Weak->Fail(TEXT("MODEL_INVALID"), FString::Printf(TEXT("The 3D model could not be read: %s"), *Err));
				return;
			}
			Weak->Timings.Add(TEXT("parse_s"), FPlatformTime::Seconds() - Weak->StageStart);
			Weak->Model = M;
			Weak->BeginBuild();
		});
	});
}

void ATwinViewerDirector::BeginBuild()
{
	State = ETwinLoadState::Building;
	Stage = TEXT("Building the walkthrough");
	BuildPhase = 0;
	BuildCursor = 0;
	StageStart = FPlatformTime::Seconds();
}

void ATwinViewerDirector::Tick(float DeltaSeconds)
{
	Super::Tick(DeltaSeconds);
	if (State == ETwinLoadState::Building)
	{
		if (BuildStep(0.012))
		{
			State = ETwinLoadState::Validating;
			Stage = TEXT("Checking the model against the floor plan");
			Progress = 0.97f;
		}
	}
	else if (State == ETwinLoadState::Validating)
	{
		ValidateBuild();
		FinishLoad();
	}
	else if (State == ETwinLoadState::Ready && Telemetry.BuildUnits > 0 && !bPollInFlight && FPlatformTime::Seconds() >= NextPoll)
	{
		PollTelemetry();
	}
}

UMaterialInterface* ATwinViewerDirector::MaterialFor(int32 GltfMaterial) const
{
	return Materials.IsValidIndex(GltfMaterial) && Materials[GltfMaterial] ? Materials[GltfMaterial].Get() : DefaultMaterial.Get();
}

bool ATwinViewerDirector::BuildStep(double Budget)
{
	const double T0 = FPlatformTime::Seconds();
	if (BuildPhase == 0)
	{
		// textures (BGRA8 + the mips made on the worker) and one material instance per glTF material
		for (const TwinGltf::FImage& Img : Model->Images)
		{
			if (Img.Mips.Num() == 0)
			{
				Textures.Add(nullptr);
				continue;
			}
			// no initial data: CreateTransient(..., data) calls UpdateResource() at once, and the render thread
			// would read the mip array while the mips below are appended (a race that crashed cooked builds)
			UTexture2D* T = UTexture2D::CreateTransient(Img.Width, Img.Height, PF_B8G8R8A8, NAME_None);
			if (!T)
			{
				Textures.Add(nullptr);
				continue;
			}
			FTexturePlatformData* PD = T->GetPlatformData();
			{
				FTexture2DMipMap& Mip0 = PD->Mips[0];
				void* Dst0 = Mip0.BulkData.Lock(LOCK_READ_WRITE);
				FMemory::Memcpy(Dst0, Img.Mips[0].GetData(), FMath::Min<int64>(Img.Mips[0].Num(), Mip0.BulkData.GetBulkDataSize()));
				Mip0.BulkData.Unlock();
			}
			for (int32 m = 1; m < Img.Mips.Num(); ++m)
			{
				const int32 W = FMath::Max(1, Img.Width >> m), H = FMath::Max(1, Img.Height >> m);
				FTexture2DMipMap* Mip = new FTexture2DMipMap(W, H, 1);
				PD->Mips.Add(Mip);
				Mip->BulkData.Lock(LOCK_READ_WRITE);
				void* Dst = Mip->BulkData.Realloc(Img.Mips[m].Num());
				FMemory::Memcpy(Dst, Img.Mips[m].GetData(), Img.Mips[m].Num());
				Mip->BulkData.Unlock();
			}
			T->SRGB = true;
			T->Filter = TF_Trilinear;
			T->AddressX = TA_Wrap;
			T->AddressY = TA_Wrap;
			T->NeverStream = true;
			T->UpdateResource();
			Textures.Add(T);
		}
		for (const TwinGltf::FMaterial& M : Model->Materials)
		{
			UMaterialInstanceDynamic* MID = UMaterialInstanceDynamic::Create(M.bBlend ? GlassBase.Get() : SurfaceBase.Get(), this);
			MID->SetVectorParameterValue(TEXT("BaseColor"), FLinearColor(M.BaseColor.R, M.BaseColor.G, M.BaseColor.B, 1.f));
			MID->SetScalarParameterValue(TEXT("Metallic"), M.Metallic);
			MID->SetScalarParameterValue(TEXT("Roughness"), M.Roughness);
			MID->SetScalarParameterValue(TEXT("Opacity"), M.BaseColor.A);
			MID->SetVectorParameterValue(TEXT("Emissive"), M.Emissive);
			if (Textures.IsValidIndex(M.BaseColorImage) && Textures[M.BaseColorImage])
			{
				MID->SetTextureParameterValue(TEXT("BaseColorTex"), Textures[M.BaseColorImage]);
				MID->SetScalarParameterValue(TEXT("UseTex"), 1.f);
			}
			Materials.Add(MID);
		}
		BuildPhase = 1;
		return false;
	}
	if (BuildPhase == 1)
	{
		const int32 N = Model->Nodes.Num();
		while (BuildCursor < N)
		{
			SpawnNode(Model->Nodes[BuildCursor++]);
			if (FPlatformTime::Seconds() - T0 > Budget)
			{
				break;
			}
		}
		Progress = 0.55f + 0.4f * (float)BuildCursor / FMath::Max(1, N);
		Stage = FString::Printf(TEXT("Building the walkthrough (%d / %d objects)"), BuildCursor, N);
		if (BuildCursor >= N)
		{
			Timings.Add(TEXT("spawn_s"), FPlatformTime::Seconds() - StageStart);
			BuildPhase = 2;
		}
		return false;
	}
	SpawnEnvironment();
	PlaceNavBounds();
	return true;
}

void ATwinViewerDirector::SpawnNode(const TwinGltf::FNode& Node)
{
	const TSharedPtr<FJsonObject> Meta = MetaByName.FindRef(Node.Name);
	FString Kind = Str(Meta, TEXT("kind"));
	if (Kind.IsEmpty() && Node.Extras.IsValid())
	{
		Kind = Str(Node.Extras, TEXT("twin_kind"));
	}
	FString Collision = Str(Meta, TEXT("collision"));
	if (Collision.IsEmpty())
	{
		Collision = Kind == TEXT("furniture") ? TEXT("box") : (Kind.StartsWith(TEXT("hvac_")) ? TEXT("none") : TEXT("complex"));
	}

	// the mesh component starts unregistered (see ATwinRuntimeMeshActor): sections + collision are filled
	// first, then it registers, so physics and the navigation octree see the real bounds
	FActorSpawnParameters P;
	P.SpawnCollisionHandlingOverride = ESpawnActorCollisionHandlingMethod::AlwaysSpawn;
	ATwinRuntimeMeshActor* A = GetWorld()->SpawnActor<ATwinRuntimeMeshActor>(ATwinRuntimeMeshActor::StaticClass(), Node.Transform, P);
	if (!A)
	{
		return;
	}
	const TwinGltf::FMesh& M = Model->Meshes[Node.Mesh];
	UProceduralMeshComponent* PMC = A->Mesh;
	// architecture: exact triangle collision; HVAC: the same, query-only (selectable, not blocking);
	// furniture: one box for movement / navigation / selection, like the editor import
	const bool bTriangles = Collision != TEXT("box");
	PMC->bUseComplexAsSimpleCollision = bTriangles;
	if (Collision == TEXT("none"))
	{
		PMC->SetCollisionEnabled(ECollisionEnabled::QueryOnly);
		PMC->SetCollisionResponseToAllChannels(ECR_Ignore);
		PMC->SetCollisionResponseToChannel(ECC_Visibility, ECR_Block);
		PMC->SetCanEverAffectNavigation(false);
	}
	else
	{
		PMC->SetCollisionProfileName(UCollisionProfile::BlockAll_ProfileName);
	}
	for (int32 i = 0; i < M.Sections.Num(); ++i)
	{
		FProcMeshSection S = M.Sections[i];
		S.bEnableCollision = bTriangles;
		PMC->SetProcMeshSection(i, S);
		PMC->SetMaterial(i, MaterialFor(M.Materials[i]));
	}
	if (!bTriangles && M.LocalBox.IsValid)
	{
		TArray<FVector> Corners;
		for (int32 c = 0; c < 8; ++c)
		{
			Corners.Add(FVector((c & 1) ? M.LocalBox.Max.X : M.LocalBox.Min.X, (c & 2) ? M.LocalBox.Max.Y : M.LocalBox.Min.Y,
			                    (c & 4) ? M.LocalBox.Max.Z : M.LocalBox.Min.Z));
		}
		PMC->AddCollisionConvexMesh(Corners);
	}
	PMC->SetCastShadow(Kind != TEXT("glass"));
	PMC->RegisterComponent();

	// the same metadata tags as an editor-imported map (see TwinMeta.h)
	auto Tag = [A](const TCHAR* Key, const FString& Value)
	{
		if (!Value.IsEmpty())
		{
			A->Tags.Add(FName(*FString::Printf(TEXT("Twin%s=%s"), Key, *Value)));
		}
	};
	A->Tags.Add(FName(TEXT("Twin")));
	A->Tags.Add(FName(TEXT("TwinRuntime")));
	Tag(TEXT("Kind"), Kind.IsEmpty() ? TEXT("unknown") : Kind);
	Tag(TEXT("Name"), Node.Name);
	Tag(TEXT("Room"), Str(Meta, TEXT("room_id")));
	Tag(TEXT("RoomName"), Str(Meta, TEXT("room_name")));
	Tag(TEXT("Label"), Str(Meta, TEXT("label")));
	if (Kind == TEXT("floor"))
	{
		const TSharedPtr<FJsonObject> Room = RoomsById.FindRef(Str(Meta, TEXT("room_id")));
		if (Room.IsValid())
		{
			const TArray<TSharedPtr<FJsonValue>>& D = Room->GetArrayField(TEXT("dims_clear_m"));
			if (D.Num() == 2)
			{
				Tag(TEXT("Dims"), FString::Printf(TEXT("%.2f x %.2f m"), D[0]->AsNumber(), D[1]->AsNumber()));
			}
			Tag(TEXT("Area"), FString::Printf(TEXT("%.1f m2"), NumOr(Room, TEXT("area_m2"), 0.0)));
			Tag(TEXT("FloorMaterial"), Str(Room, TEXT("floor_material")));
			FVector Spawn;
			if (Vec(Room, TEXT("spawn_cm"), Spawn))
			{
				Tag(TEXT("Spawn"), FString::Printf(TEXT("%.1f,%.1f,%.1f"), Spawn.X, Spawn.Y, Spawn.Z));
			}
		}
	}
	else if (Kind == TEXT("furniture"))
	{
		Tag(TEXT("Id"), Str(Meta, TEXT("furniture_id")));
		Tag(TEXT("Asset"), Str(Meta, TEXT("asset_id")));
		Tag(TEXT("Size"), Str(Meta, TEXT("size_text")));
		Tag(TEXT("Category"), Str(Meta, TEXT("category")));
	}
	else if (Kind.StartsWith(TEXT("hvac_")))
	{
		Tag(TEXT("Hvac"), Str(Meta, TEXT("hvac_id")));
		Tag(TEXT("Tag"), Str(Meta, TEXT("tag")).IsEmpty() ? Str(Meta, TEXT("unit_tag")) : Str(Meta, TEXT("tag")));
		Tag(TEXT("Unit"), Str(Meta, TEXT("unit_id")));
		Tag(TEXT("UnitKind"), Str(Meta, TEXT("unit_kind")));
		Tag(TEXT("System"), Str(Meta, TEXT("system")));
		const double DesignLps = NumOr(Meta, TEXT("airflow_lps"), -1.0);
		if (DesignLps > 0)
		{
			Tag(TEXT("Airflow"), FString::Printf(TEXT("%.0f"), DesignLps));
		}
	}
	NodeActors.Add(A);
	ExpectedBox.Add(BoxField(Meta, TEXT("bounds_cm")));
	NodeNames.Add(Node.Name);
	++ActorsSpawned;
	TrianglesSpawned += M.Triangles;
}

void ATwinViewerDirector::SpawnEnvironment()
{
	UWorld* W = GetWorld();
	const FBox B = BoxField(Manifest, TEXT("bounds_cm"));
	for (const TSharedPtr<FJsonValue>& V : Manifest->GetArrayField(TEXT("lights")))
	{
		const TSharedPtr<FJsonObject> L = V->AsObject();
		const FColor Color = HexColor(Str(L, TEXT("color")));
		if (Str(L, TEXT("type")) == TEXT("directional"))
		{
			ADirectionalLight* Sun = nullptr;
			for (TActorIterator<ADirectionalLight> It(W); It; ++It)
			{
				Sun = *It;
				break;
			}
			if (Sun)
			{
				Sun->SetActorRotation(FRotator(NumOr(L, TEXT("pitch_deg"), -45.0), NumOr(L, TEXT("yaw_deg"), 0.0), 0.0));
				Sun->GetLightComponent()->SetLightColor(Color);
			}
			continue;
		}
		FVector Loc;
		if (!Vec(L, TEXT("location_cm"), Loc))
		{
			continue;
		}
		APointLight* PL = W->SpawnActorDeferred<APointLight>(APointLight::StaticClass(), FTransform(Loc - FVector(0, 0, 45.0)));
		if (!PL)
		{
			continue;
		}
		UPointLightComponent* C = PL->PointLightComponent;
		C->SetMobility(EComponentMobility::Movable);
		C->IntensityUnits = ELightUnits::Lumens;
		C->Intensity = (float)NumOr(L, TEXT("lumens"), 1500.0);
		C->AttenuationRadius = (float)NumOr(L, TEXT("radius_cm"), 800.0);
		C->LightColor = Color;
		C->SourceRadius = 30.f;
		C->SoftSourceRadius = 60.f;
		C->CastShadows = false;  // dozens of shadowed point lights are too slow for a laptop; the sun casts shadows
		PL->FinishSpawning(FTransform(Loc - FVector(0, 0, 45.0)));
		Spawned.Add(PL);
	}

	// a ground plane around the building (walkable outside, gives the overview a base)
	if (B.IsValid)
	{
		const FVector C = B.GetCenter();
		const double Half = FMath::Max(B.GetSize().X, B.GetSize().Y) * 3.0;
		ATwinRuntimeMeshActor* G = W->SpawnActor<ATwinRuntimeMeshActor>(ATwinRuntimeMeshActor::StaticClass(),
		                                                                 FTransform(FVector(C.X, C.Y, B.Min.Z - 1.0)));
		if (G)
		{
			FProcMeshSection S;
			for (int32 i = 0; i < 4; ++i)
			{
				FProcMeshVertex& Vx = S.ProcVertexBuffer.AddDefaulted_GetRef();
				Vx.Position = FVector((i == 1 || i == 2) ? Half : -Half, (i >= 2) ? Half : -Half, 0.0);
				Vx.Normal = FVector::UpVector;
				Vx.UV0 = FVector2D(Vx.Position.X / 200.0, Vx.Position.Y / 200.0);
				S.SectionLocalBox += Vx.Position;
			}
			S.ProcIndexBuffer = {0, 2, 1, 0, 3, 2};  // Unreal front faces wind clockwise seen from the front
			S.bEnableCollision = true;
			G->Mesh->bUseComplexAsSimpleCollision = true;
			G->Mesh->SetCollisionProfileName(UCollisionProfile::BlockAll_ProfileName);
			G->Mesh->SetProcMeshSection(0, S);
			UMaterialInstanceDynamic* GM = UMaterialInstanceDynamic::Create(SurfaceBase, this);
			GM->SetVectorParameterValue(TEXT("BaseColor"), FLinearColor(0.32f, 0.33f, 0.31f));
			GM->SetScalarParameterValue(TEXT("Roughness"), 0.95f);
			G->Mesh->SetMaterial(0, GM);
			G->Mesh->RegisterComponent();
			G->Tags.Add(FName(TEXT("TwinGround")));
			Spawned.Add(G);
		}
	}

	// project info actor (read by the HUD like the editor importer's TwinProjectInfo)
	AActor* Info = W->SpawnActor<AActor>(AActor::StaticClass(), FTransform::Identity);
	if (Info)
	{
		const TSharedPtr<FJsonObject> Prj = Manifest->GetObjectField(TEXT("project"));
		Info->Tags.Add(FName(TEXT("Twin")));
		Info->Tags.Add(FName(TEXT("TwinKind=project")));
		Info->Tags.Add(FName(*(TEXT("TwinName=") + Str(Prj, TEXT("name")))));
		Info->Tags.Add(FName(*(TEXT("TwinBuild=") + Launch.BuildId)));
		Info->Tags.Add(FName(*FString::Printf(TEXT("TwinRooms=%d"), RoomsById.Num())));
		Spawned.Add(Info);
	}
}

void ATwinViewerDirector::PlaceNavBounds()
{
	const FBox B = BoxField(Manifest, TEXT("bounds_cm"));
	ANavMeshBoundsVolume* Vol = nullptr;
	for (TActorIterator<ANavMeshBoundsVolume> It(GetWorld()); It; ++It)
	{
		Vol = *It;
		break;
	}
	if (!Vol || !B.IsValid)
	{
		UE_LOG(LogTwinViewer, Warning, TEXT("[twin] no NavMeshBoundsVolume in the viewer map: click-to-walk is unavailable"));
		return;
	}
	// the viewer map's volume has a 100 cm box brush at the origin; move + scale it over this building
	const FVector Size = B.GetSize() + FVector(400.0, 400.0, 0.0);
	const FVector Center(B.GetCenter().X, B.GetCenter().Y, B.Min.Z + 150.0);
	Vol->GetRootComponent()->SetMobility(EComponentMobility::Movable);
	Vol->SetActorLocation(Center);
	Vol->SetActorScale3D(FVector(Size.X / 100.0, Size.Y / 100.0, FMath::Max(600.0, B.GetSize().Z + 300.0) / 100.0));
	UNavigationSystemV1* Nav = FNavigationSystem::GetCurrent<UNavigationSystemV1>(GetWorld());
	if (!Nav)
	{
		UE_LOG(LogTwinViewer, Warning, TEXT("[twin] no navigation system: click-to-walk is unavailable"));
		return;
	}
	Nav->OnNavigationBoundsUpdated(Vol);
	// the empty viewer level may start without navigation data; create it for the configured agent
	ANavigationData* Data = Nav->GetDefaultNavDataInstance(FNavigationSystem::DontCreate);
	if (!Data)
	{
		Data = Nav->GetDefaultNavDataInstance(FNavigationSystem::Create);
		if (Data)
		{
			Data->RebuildAll();
		}
	}
	int32 NavDatas = 0;
	for (TActorIterator<ANavigationData> It(GetWorld()); It; ++It)
	{
		++NavDatas;
	}
	UE_LOG(LogTwinViewer, Display, TEXT("[twin] nav bounds %s, %d bounds registered, nav data %s (%d in world)"),
	       *Vol->GetComponentsBoundingBox(true).ToString(), Nav->GetNavigationBounds().Num(), *GetNameSafe(Data), NavDatas);
}

void ATwinViewerDirector::ValidateBuild()
{
	const double T0 = FPlatformTime::Seconds();
	TSharedPtr<FJsonObject> R = MakeShared<FJsonObject>();
	TArray<TSharedPtr<FJsonValue>> Failures, Unmatched;
	double Worst = 0.0;
	int32 Checked = 0, NFail = 0;
	TSet<FString> Seen;
	FBox ArchUnion(ForceInit);
	int32 Floors = 0;
	for (int32 i = 0; i < NodeActors.Num(); ++i)
	{
		AActor* A = NodeActors[i];
		Seen.Add(NodeNames[i]);
		if (!IsValid(A))
		{
			continue;
		}
		const FBox Got = A->GetComponentsBoundingBox(true);
		const TSharedPtr<FJsonObject> Meta = MetaByName.FindRef(NodeNames[i]);
		if (Meta.IsValid() && Str(Meta, TEXT("kind")) != TEXT("furniture"))
		{
			ArchUnion += Got;
			Floors += Str(Meta, TEXT("kind")) == TEXT("floor") ? 1 : 0;
		}
		if (!ExpectedBox[i].IsValid)
		{
			Unmatched.Add(MakeShared<FJsonValueString>(NodeNames[i]));
			continue;
		}
		const double Dev = BoxDeviation(Got, ExpectedBox[i]);
		Worst = FMath::Max(Worst, Dev);
		++Checked;
		if (Dev > TolCm)
		{
			++NFail;
			if (Failures.Num() < 30)
			{
				TSharedPtr<FJsonObject> F = MakeShared<FJsonObject>();
				F->SetStringField(TEXT("object"), NodeNames[i]);
				F->SetNumberField(TEXT("deviation_cm"), Dev);
				F->SetArrayField(TEXT("expected"), BoxJson(ExpectedBox[i]));
				F->SetArrayField(TEXT("got"), BoxJson(Got));
				Failures.Add(MakeShared<FJsonValueObject>(F));
			}
		}
	}
	TArray<TSharedPtr<FJsonValue>> Missing;
	for (const auto& Pair : MetaByName)
	{
		if (!Seen.Contains(Pair.Key) && Missing.Num() < 30)
		{
			Missing.Add(MakeShared<FJsonValueString>(Pair.Key));
		}
	}
	const int32 MissingCount = MetaByName.Num() - (Seen.Num() - Unmatched.Num());
	const FBox Expected = BoxField(Manifest, TEXT("bounds_cm"));
	const double BoundsDev = (Expected.IsValid && ArchUnion.IsValid) ? BoxDeviation(Expected, ArchUnion) : -1.0;

	R->SetStringField(TEXT("project_id"), Launch.ProjectId);
	R->SetStringField(TEXT("build_id"), Launch.BuildId);
	R->SetStringField(TEXT("checked_in"), TEXT("Unreal Engine runtime (packaged viewer / -game), actor bounds vs scene model"));
	R->SetNumberField(TEXT("tolerance_cm"), TolCm);
	R->SetNumberField(TEXT("objects_expected"), MetaByName.Num());
	R->SetNumberField(TEXT("actors"), NodeActors.Num());
	R->SetNumberField(TEXT("checked"), Checked);
	R->SetNumberField(TEXT("failures"), NFail);
	R->SetArrayField(TEXT("failure_examples"), Failures);
	R->SetNumberField(TEXT("max_deviation_cm"), Worst);
	R->SetNumberField(TEXT("missing_count"), FMath::Max(0, MissingCount));
	R->SetArrayField(TEXT("missing"), Missing);
	R->SetArrayField(TEXT("unmatched_nodes"), Unmatched);
	R->SetNumberField(TEXT("rooms_expected"), RoomsById.Num());
	R->SetNumberField(TEXT("rooms_with_floor"), Floors);
	R->SetArrayField(TEXT("building_bounds_expected_cm"), BoxJson(Expected));
	R->SetArrayField(TEXT("building_bounds_got_cm"), BoxJson(ArchUnion));
	R->SetNumberField(TEXT("building_bounds_deviation_cm"), BoundsDev);
	R->SetNumberField(TEXT("triangles"), (double)TrianglesSpawned);
	R->SetNumberField(TEXT("textures"), Textures.Num());
	R->SetNumberField(TEXT("materials"), Materials.Num());
	R->SetBoolField(TEXT("viewer_materials_found"), SurfaceBase && SurfaceBase->GetPathName() == SurfacePath);
	R->SetBoolField(TEXT("from_cache"), bFromCache);
	R->SetNumberField(TEXT("model_bytes"), (double)BytesTotal);
	R->SetBoolField(TEXT("ok"), NFail == 0 && MissingCount <= 0 && Unmatched.Num() == 0 && BoundsDev >= 0 && BoundsDev <= TolCm);
	const TSharedPtr<FJsonObject>* Pre = nullptr;
	if (Manifest->TryGetObjectField(TEXT("preflight"), Pre))
	{
		R->SetObjectField(TEXT("server_preflight"), *Pre);
	}
	Validation = R;
	MaxDeviationCm = Worst;
	ValidationChecked = Checked;
	ValidationFailures = NFail + FMath::Max(0, MissingCount) + Unmatched.Num();
	Timings.Add(TEXT("validate_s"), FPlatformTime::Seconds() - T0);

	FString Text;
	const TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Text);
	FJsonSerializer::Serialize(R.ToSharedRef(), Writer);
	const FString Path = FPaths::ProjectSavedDir() / TEXT("TwinViewer") / FString::Printf(TEXT("validation_%s.json"), *Launch.BuildId);
	FFileHelper::SaveStringToFile(Text, *Path, FFileHelper::EEncodingOptions::ForceUTF8WithoutBOM);
	UE_LOG(LogTwinViewer, Display, TEXT("[twin] validation: %d checked, %d failures, max %.4f cm (%s)"), Checked, ValidationFailures, Worst, *Path);
}

void ATwinViewerDirector::FinishLoad()
{
	Timings.Add(TEXT("total_s"), FPlatformTime::Seconds() - LoadStart);
	State = ETwinLoadState::Ready;
	Stage = TEXT("Ready");
	Progress = 1.f;
	++LoadCount;
	Model.Reset();  // geometry now lives in the components
	NextPoll = 0.0;
	Telemetry.BuildUnits = Manifest->GetObjectField(TEXT("hvac"))->GetArrayField(TEXT("units")).Num();
	UE_LOG(LogTwinViewer, Display, TEXT("[twin] ready: %s, %d actors, %lld triangles in %.1f s"), *Launch.BuildId, ActorsSpawned,
	       TrianglesSpawned, Timings[TEXT("total_s")]);
	OnLoaded.Broadcast();
	if (Telemetry.BuildUnits > 0)
	{
		FetchAirflow();
	}
}

// ------------------------------------------------------------------------------------------------
// HVAC: telemetry + airflow illustration
// ------------------------------------------------------------------------------------------------

void ATwinViewerDirector::PollTelemetry()
{
	bPollInFlight = true;
	const int32 Serial = RequestSerial;
	const double Every = NumOr(Manifest, TEXT("poll_seconds"), 4.0);
	GetJson(BuildPath(TEXT("hvac/live")), [this, Serial, Every](TSharedPtr<FJsonObject> J)
	{
		if (Serial != RequestSerial)
		{
			return;
		}
		bPollInFlight = false;
		NextPoll = FPlatformTime::Seconds() + Every;
		ApplyTelemetry(J);
	}, [this, Serial, Every](const FString& Code, const FString& Message)
	{
		if (Serial != RequestSerial)
		{
			return;
		}
		bPollInFlight = false;
		NextPoll = FPlatformTime::Seconds() + Every;
		++Telemetry.Failures;
		Telemetry.Error = FString::Printf(TEXT("%s: %s"), *Code, *Message);
	}, 10.f);
}

void ATwinViewerDirector::ApplyTelemetry(const TSharedPtr<FJsonObject>& J)
{
	FTwinTelemetry T;
	T.BuildUnits = Telemetry.BuildUnits;
	T.Source = Str(J, TEXT("source"));
	T.Label = Str(J, TEXT("label"));
	J->TryGetBoolField(TEXT("measured"), T.bMeasured);
	T.DataTs = NumOr(J, TEXT("ts"), 0.0);
	T.ReceivedAt = FPlatformTime::Seconds();
	const TSharedPtr<FJsonObject>* Site = nullptr;
	if (J->TryGetObjectField(TEXT("site"), Site))
	{
		T.OutdoorC = (float)NumOr(*Site, TEXT("outdoor_c"), NAN);
	}
	auto F = [](const TSharedPtr<FJsonObject>& O, const TCHAR* K) { return (float)NumOr(O, K, NAN); };
	for (const TSharedPtr<FJsonValue>& V : J->GetArrayField(TEXT("units")))
	{
		const TSharedPtr<FJsonObject> U = V->AsObject();
		FTwinHvacReading Rd;
		Rd.UnitId = Str(U, TEXT("unit_id"));
		Rd.Tag = Str(U, TEXT("tag"));
		Rd.Mode = Str(U, TEXT("mode"));
		Rd.Alarm = Str(U, TEXT("alarm"));
		U->TryGetBoolField(TEXT("on"), Rd.bOn);
		Rd.SetpointC = F(U, TEXT("setpoint_c"));
		Rd.SupplyC = F(U, TEXT("supply_air_c"));
		Rd.ReturnC = F(U, TEXT("return_air_c"));
		Rd.RoomC = F(U, TEXT("room_temp_c"));
		Rd.Fan = F(U, TEXT("fan_speed"));
		Rd.AirflowLps = F(U, TEXT("airflow_lps"));
		Rd.Ts = NumOr(U, TEXT("ts"), 0.0);
		T.Units.Add(Rd.UnitId, Rd);
	}
	const TSharedPtr<FJsonObject>* Match = nullptr;
	if (J->TryGetObjectField(TEXT("matching"), Match))
	{
		T.Missing = (*Match)->GetArrayField(TEXT("missing")).Num();
		T.Unknown = (*Match)->GetArrayField(TEXT("unknown")).Num();
	}
	T.Error = Str(J, TEXT("reason"));
	Telemetry = MoveTemp(T);
}

void ATwinViewerDirector::FetchAirflow()
{
	const int32 Serial = RequestSerial;
	GetJson(BuildPath(TEXT("hvac/airflow")), [this, Serial](TSharedPtr<FJsonObject> J)
	{
		if (Serial != RequestSerial)
		{
			return;
		}
		Airflow = J;
		if (!AirflowActor)
		{
			AirflowActor = GetWorld()->SpawnActor<ATwinAirflowActor>(ATwinAirflowActor::StaticClass(), FTransform::Identity);
		}
		if (AirflowActor)
		{
			AirflowActor->Build(J, this);
		}
	}, [this, Serial](const FString& Code, const FString& Message)
	{
		if (Serial == RequestSerial)
		{
			AirflowError = FString::Printf(TEXT("%s: %s"), *Code, *Message);
		}
	});
}

void ATwinViewerDirector::SetAirflowVisible(bool bVisible)
{
	if (AirflowActor)
	{
		AirflowActor->SetShown(bVisible);
	}
}

bool ATwinViewerDirector::IsAirflowVisible() const
{
	return AirflowActor && AirflowActor->IsShown();
}

TSharedPtr<FJsonObject> ATwinViewerDirector::ObjectMeta(const FString& Name) const
{
	return MetaByName.FindRef(Name);
}
